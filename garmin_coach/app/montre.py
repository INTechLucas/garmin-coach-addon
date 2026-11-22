"""Envoi des séances du coach vers la montre via le calendrier Garmin Connect.

L'API personnelle Garmin est en lecture seule : l'écriture passe par la bibliothèque non officielle
`garminconnect` (connexion de l'appli mobile). L'athlète se connecte une fois depuis le panneau ;
seuls les jetons sont conservés (dans /data, privé à l'add-on), jamais le mot de passe.

- `build_workout` traduit la structure d'une séance (format simple écrit par le coach) en entraînement
  Garmin, en appliquant les préférences de l'athlète :
    course   : l'échauffement se termine par un appui sur LAP, le reste s'enchaîne automatiquement ;
    natation : les repos se terminent par LAP (s'adapter à la piscine), les nages à la distance ;
    renfo    : exercice -> LAP quand la série est faite, repos chronométrés ;
    crossfit : blocs chronométrés (AMRAP, EMOM, Tabata…), « for time » sur LAP.
- `MontreSync` tient le calendrier Garmin aligné sur les fiches séance : création, mise à jour,
  déplacement, nettoyage des semaines terminées. État dans <coach>/montre.json (aucun secret).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .fiches import FicheStore

LOG = logging.getLogger("garmin_coach.montre")
PREFIXE = "Coach · "

SPORTS = {
    "course": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
    "natation": {"sportTypeId": 4, "sportTypeKey": "swimming", "displayOrder": 3},
    "renfo": {"sportTypeId": 5, "sportTypeKey": "strength_training", "displayOrder": 5},
    "crossfit": {"sportTypeId": 9, "sportTypeKey": "hiit", "displayOrder": 9},
    "cardio": {"sportTypeId": 6, "sportTypeKey": "cardio_training", "displayOrder": 6},
}
STEP = {
    "echauffement": (1, "warmup"), "retour": (2, "cooldown"), "effort": (3, "interval"),
    "recup": (4, "recovery"), "repos": (5, "rest"), "autre": (7, "other"),
}
COND = {"lap": (1, "lap.button"), "time": (2, "time"), "distance": (3, "distance"),
        "iterations": (7, "iterations"), "reps": (10, "reps")}
NO_TARGET = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}
# Repli si /workout-service/workout/types ne répond pas : clés Garmin connues
STROKES = {"libre": "free", "crawl": "free", "dos": "backstroke", "brasse": "breaststroke",
           "papillon": "fly", "pap": "fly", "4n": "individual_medley", "educatif": "drill",
           "jambes": "drill", "mixte": "mixed", "toutes": "any_stroke"}
STROKE_IDS = {"any_stroke": 1, "backstroke": 2, "breaststroke": 3, "drill": 4, "fly": 5, "free": 6,
              "individual_medley": 7, "mixed": 8}
EQUIP = {"palmes": "fins", "planche": "kickboard", "plaquettes": "paddles", "pull": "pull_buoy",
         "pull-buoy": "pull_buoy", "tuba": "snorkel"}
EQUIP_IDS = {"fins": 1, "kickboard": 2, "paddles": 3, "pull_buoy": 4, "snorkel": 5}


class StructureError(ValueError):
    pass


def _pace(s: str) -> float:
    m = re.fullmatch(r"\s*(\d{1,2})[:'](\d{2})\s*", str(s))
    if not m:
        raise StructureError(f"allure attendue au format m:ss (/km), reçu {s!r}")
    return 1000 / (int(m.group(1)) * 60 + int(m.group(2)))


def _fmt(sec: float) -> str:
    sec = int(round(sec))
    if sec < 60:
        return f"{sec} s"
    return f"{sec // 60}'{sec % 60:02d}" if sec % 60 else f"{sec // 60}'"


def resolve_exercise(name: str) -> dict[str, str]:
    """« Back Squat », « Barbell Back Squat » ou « SQUAT/BARBELL_BACK_SQUAT »."""
    from garminconnect import exercises  # catalogue officiel de l'éditeur Garmin Connect

    if "/" in name:
        cat, _, ex = name.partition("/")
        for e in exercises.EXERCISES:
            if e["category"] == cat.strip().upper() and e["exercise"] == ex.strip().upper():
                return e
    e = exercises.resolve(name)
    if e:
        return e
    low = {x["name"].lower(): x for x in exercises.EXERCISES}
    if name.lower() in low:
        return low[name.lower()]
    sugg = difflib.get_close_matches(name.lower(), list(low), n=5, cutoff=0.5) or \
        [x["name"] for x in exercises.find(name.split()[0])[:5]]
    raise StructureError(f"exercice inconnu dans le catalogue Garmin : {name!r}"
                         + (f" (proches : {', '.join(sugg)})" if sugg else ""))


class _Builder:
    def __init__(self, sport: str, types: dict[str, dict[str, int]] | None) -> None:
        self.sport = sport
        self.types = types or {}
        self.order = 0
        self.duree = 0.0

    def _id(self, family: str, key: str, fallback: dict[str, int]) -> int:
        return self.types.get(family, {}).get(key) or fallback[key]

    def _cond(self, key: str) -> dict[str, Any]:
        i, k = COND[key]
        return {"conditionTypeId": i, "conditionTypeKey": k, "displayOrder": i, "displayable": True}

    def step(self, e: dict[str, Any], mult: int) -> dict[str, Any]:
        t = e.get("type")
        if t == "repeter":
            return self.repeat(e, mult)
        if t not in STEP:
            raise StructureError(f"type d'étape inconnu : {t!r} (echauffement, effort, recup, repos, retour, "
                                 f"autre, repeter)")
        self.order += 1
        sid, skey = STEP[t]
        s: dict[str, Any] = {"type": "ExecutableStepDTO", "stepOrder": self.order,
                             "stepType": {"stepTypeId": sid, "stepTypeKey": skey, "displayOrder": sid},
                             "targetType": dict(NO_TARGET)}
        secs = (e.get("duree_min") or 0) * 60 + (e.get("duree_s") or 0)
        dist = e.get("distance_m")
        note = (e.get("note") or "").strip()

        # --- condition de fin, selon le sport et les préférences de l'athlète
        fin = e.get("fin")
        if self.sport == "course" and t == "echauffement":
            fin = "lap"
            if secs:
                note = f"≈ {_fmt(secs)} puis LAP quand tu es à un endroit sûr" + (f" · {note}" if note else "")
        elif self.sport == "natation" and t in ("repos", "recup"):
            fin = "lap"
            if secs:
                note = f"Repos ≈ {_fmt(secs)}, LAP pour repartir" + (f" · {note}" if note else "")
        elif self.sport == "renfo" and t == "effort" and e.get("reps"):
            fin = "reps"
        if fin == "lap":
            s["endCondition"] = self._cond("lap")
            self.duree += secs * mult
        elif fin == "reps":
            s["endCondition"], s["endConditionValue"] = self._cond("reps"), float(e["reps"])
            self.duree += (secs or 40) * mult
        elif dist:
            s["endCondition"], s["endConditionValue"] = self._cond("distance"), float(dist)
            self.duree += (dist / (0.9 if self.sport == "natation" else 3.0)) * mult
        elif secs:
            s["endCondition"], s["endConditionValue"] = self._cond("time"), float(secs)
            self.duree += secs * mult
        else:
            raise StructureError(f"étape {t!r} sans fin : donner duree_min/duree_s, distance_m, reps ou fin='lap'")

        # --- cible
        c = e.get("cible") or {}
        if self.sport == "course" and c:
            if "allure" in c:
                a = c["allure"]
                lo, hi = (a, a) if isinstance(a, str) else (a[0], a[-1])
                v1, v2 = sorted((_pace(lo), _pace(hi)))
                s["targetType"] = {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone", "displayOrder": 6}
                s["targetValueOne"], s["targetValueTwo"] = round(v1, 4), round(v2, 4)
            elif "fc" in c or "fc_max" in c:
                lo, hi = (c["fc"][0], c["fc"][-1]) if "fc" in c else (int(c["fc_max"]) - 25, int(c["fc_max"]))
                s["targetType"] = {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone",
                                   "displayOrder": 4}
                s["targetValueOne"], s["targetValueTwo"] = float(min(lo, hi)), float(max(lo, hi))

        # --- natation : nage et matériel
        if self.sport == "natation" and t not in ("repos",):
            nage = STROKES.get(str(e.get("nage", "libre")).lower(), "free")
            s["strokeType"] = {"strokeTypeId": self._id("stroke", nage, STROKE_IDS), "strokeTypeKey": nage}
            mat = e.get("materiel")
            if mat:
                key = EQUIP.get(str(mat).lower())
                if not key:
                    raise StructureError(f"matériel inconnu : {mat!r} ({', '.join(EQUIP)})")
                s["equipmentType"] = {"equipmentTypeId": self._id("equipment", key, EQUIP_IDS),
                                      "equipmentTypeKey": key}

        # --- renfo / crossfit : exercice du catalogue Garmin
        if e.get("exercice"):
            if self.sport not in ("renfo", "crossfit", "cardio"):
                raise StructureError("exercice réservé aux séances renfo ou crossfit")
            ex = resolve_exercise(str(e["exercice"]))
            s["category"], s["exerciseName"] = ex["category"], ex["exercise"]
            if e.get("charge_kg"):
                s["weightValue"] = float(e["charge_kg"]) * 1000.0
                s["weightUnit"] = {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0}
        if note:
            s["description"] = note[:200]
        return s

    def repeat(self, e: dict[str, Any], mult: int) -> dict[str, Any]:
        n = int(e.get("fois") or 0)
        if not 1 <= n <= 99:
            raise StructureError("repeter : 'fois' entre 1 et 99")
        if not e.get("etapes"):
            raise StructureError("repeter : liste 'etapes' vide")
        self.order += 1
        g: dict[str, Any] = {
            "type": "RepeatGroupDTO", "stepOrder": self.order,
            "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
            "numberOfIterations": n, "smartRepeat": False,
            "endCondition": {**self._cond("iterations"), "displayable": False}, "endConditionValue": float(n),
        }
        g["workoutSteps"] = [self.step(x, mult * n) for x in e["etapes"]]
        if e.get("sans_dernier_repos"):
            g["skipLastRestStep"] = True
        return g


def build_workout(titre: str, structure: dict[str, Any], resume: str | None = None,
                  types: dict[str, dict[str, int]] | None = None) -> dict[str, Any]:
    if not isinstance(structure, dict):
        raise StructureError("structure : objet JSON attendu")
    sport = structure.get("sport")
    if sport not in SPORTS:
        raise StructureError(f"structure.sport doit être l'un de : {', '.join(SPORTS)}")
    etapes = structure.get("etapes")
    if not isinstance(etapes, list) or not etapes:
        raise StructureError("structure.etapes : liste d'étapes attendue")
    b = _Builder(sport, types)
    steps = [b.step(e, 1) for e in etapes]
    sport_type = SPORTS[sport]
    w: dict[str, Any] = {
        "workoutName": (PREFIXE + titre)[:60],
        "sportType": sport_type,
        "estimatedDurationInSecs": int(b.duree),
        "workoutSegments": [{"segmentOrder": 1, "sportType": sport_type, "workoutSteps": steps}],
        "author": {},
    }
    if resume:
        w["description"] = resume[:500]
    if sport == "natation":
        w["poolLength"] = float(structure.get("piscine_m") or 25)
        w["poolLengthUnit"] = {"unitId": 1, "unitKey": "meter", "factor": 100.0}
    return w


def structure_hash(titre: str, structure: dict[str, Any], resume: str | None) -> str:
    raw = json.dumps([titre, structure, resume], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _parse_types(raw: Any) -> dict[str, dict[str, int]]:
    """Indexe les ids de /workout-service/workout/types (nages, matériel) par clé."""
    out: dict[str, dict[str, int]] = {"stroke": {}, "equipment": {}}

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for fam in ("stroke", "equipment"):
                k, i = x.get(f"{fam}TypeKey"), x.get(f"{fam}TypeId")
                if isinstance(k, str) and isinstance(i, int):
                    out[fam][k] = i
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(raw)
    return out


class MontreSync:
    """Aligne le calendrier Garmin Connect sur les fiches séance du coach."""

    def __init__(self, coach_dir: Path, token_dir: Path, fiches: FicheStore, suivi: Any) -> None:
        self.path = coach_dir / "montre.json"
        self.token_dir = token_dir
        self.fiches, self.suivi = fiches, suivi
        self.lock = threading.RLock()
        self._kick = threading.Event()
        self._client: Any = None
        self._pending_mfa: tuple[float, Any] | None = None
        self._types: dict[str, dict[str, int]] | None = None
        try:
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.state = {}
        self.state.setdefault("seances", {})
        self.state.setdefault("derniere_synchro", None)
        self.state.setdefault("erreur", None)

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    # ---- connexion ------------------------------------------------------------
    @property
    def token_file(self) -> Path:
        return self.token_dir / "garmin_tokens.json"

    def connecte(self) -> bool:
        return self.token_file.exists()

    def _garmin(self) -> Any:
        from garminconnect import Garmin  # import tardif : seule la partie montre en dépend

        return Garmin

    def client(self) -> Any:
        with self.lock:
            if self._client is None:
                if not self.connecte():
                    raise RuntimeError("pas connecté à Garmin Connect")
                g = self._garmin()()
                g.login(str(self.token_dir))  # charge les jetons, les rafraîchit et les réenregistre
                self._client = g
            return self._client

    def connexion(self, email: str, password: str) -> dict[str, Any]:
        email, password = (email or "").strip(), password or ""
        if not email or not password:
            raise ValueError("e-mail et mot de passe requis")
        g = self._garmin()(email, password, return_on_mfa=True)
        status, state = g.login()
        if status == "needs_mfa":
            with self.lock:
                self._pending_mfa = (time.monotonic(), state)
            return {"mfa": True}
        self._finish(g)
        return {"connecte": True}

    def mfa(self, code: str) -> dict[str, Any]:
        with self.lock:
            pending = self._pending_mfa
        if not pending or time.monotonic() - pending[0] > 600:
            raise ValueError("pas de connexion en attente de code (ou code expiré) : recommence la connexion")
        g = self._garmin()()
        g.resume_login(pending[1], re.sub(r"\D", "", code or ""))
        self._finish(g)
        return {"connecte": True}

    def _finish(self, g: Any) -> None:
        self.token_dir.mkdir(parents=True, exist_ok=True)
        g.client.dump(str(self.token_dir))
        with self.lock:
            self._pending_mfa = None
            self._client = None
            self.state["erreur"] = None
            self._save()
        LOG.info("Connexion Garmin Connect enregistrée (jetons uniquement)")
        self.kick()

    def deconnexion(self) -> None:
        with self.lock:
            self.token_file.unlink(missing_ok=True)
            self._client = None

    # ---- synchronisation ---------------------------------------------------------
    def kick(self) -> None:
        self._kick.set()

    def loop(self, interval_s: int = 1800) -> None:
        time.sleep(15)
        while True:
            try:
                self.sync()
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Synchro montre en échec : %s", exc)
                self.fail(exc)
            self._kick.wait(interval_s)
            self._kick.clear()
            time.sleep(3)  # regroupe les modifications rapprochées

    def _types_ids(self, g: Any) -> dict[str, dict[str, int]]:
        if self._types is None:
            try:
                self._types = _parse_types(g.get_workout_types())
            except Exception as exc:  # noqa: BLE001
                LOG.info("Types d'entraînement Garmin indisponibles (%s) : ids par défaut", exc)
                self._types = {}
        return self._types

    def _upload(self, g: Any, payload: dict[str, Any], wid: int | None) -> int:
        def send(p: dict[str, Any]) -> Any:
            return g.update_workout(wid, p) if wid else g.upload_workout(p)

        try:
            r = send(payload)
        except Exception:
            alt = dict(payload)
            if alt.get("sportType", {}).get("sportTypeKey") == "hiit":  # montre/compte sans HIIT : cardio
                alt["sportType"] = SPORTS["cardio"]
                alt["workoutSegments"] = [{**s, "sportType": SPORTS["cardio"]} for s in alt["workoutSegments"]]
            elif "poolLength" in alt:
                alt.pop("poolLength"), alt.pop("poolLengthUnit", None)
            else:
                raise
            r = send(alt)
        new_id = (r or {}).get("workoutId") if isinstance(r, dict) else None
        return int(new_id or wid)

    def sync(self, today: date | None = None) -> dict[str, Any]:
        if not self.connecte():
            return {"connecte": False}
        today = today or date.today()
        monday = today - timedelta(days=today.weekday())
        horizon = monday + timedelta(days=13)
        with self.lock:
            g = self.client()
            types = self._types_ids(g)
            etat = self.state["seances"]
            voulues: dict[str, dict[str, Any]] = {}
            for f in self.fiches.list("seance"):
                d = f.get("date")
                if not f.get("structure") or not d or not monday.isoformat() <= d <= horizon.isoformat():
                    continue
                voulues[f["id"]] = f
            erreurs = 0
            for sid, f in voulues.items():
                e = etat.setdefault(sid, {})
                if self.suivi and self.suivi.est_realisee(sid):
                    continue  # séance faite : on ne touche plus à rien
                if f["date"] < today.isoformat() and e.get("schedule_id"):
                    continue  # jour passé non fait : reste dans la bibliothèque de la montre
                try:
                    h = structure_hash(f["titre"], f["structure"], f.get("resume"))
                    if not e.get("workout_id") or e.get("hash") != h:
                        payload = build_workout(f["titre"], f["structure"], f.get("resume"), types)
                        e["workout_id"] = self._upload(g, payload, e.get("workout_id"))
                        e["hash"] = h
                    if e.get("date") != f["date"] or not e.get("schedule_id"):
                        if e.get("schedule_id"):
                            try:
                                g.unschedule_workout(e["schedule_id"])
                            except Exception as exc:  # noqa: BLE001
                                LOG.info("Déprogrammation %s : %s", sid, exc)
                        r = g.schedule_workout(e["workout_id"], f["date"])
                        e["schedule_id"] = (r or {}).get("workoutScheduleId") or (r or {}).get("scheduleId") \
                            or (r or {}).get("id")
                        e["date"] = f["date"]
                    e.update(etat_envoi="envoyee", erreur=None, maj=_now())
                except StructureError as exc:
                    erreurs += 1
                    e.update(etat_envoi="erreur", erreur=f"structure : {exc}", maj=_now())
                except Exception as exc:  # noqa: BLE001
                    erreurs += 1
                    e.update(etat_envoi="erreur", erreur=str(exc)[:300], maj=_now())
                    LOG.warning("Envoi de %s en échec : %s", sid, exc)
            # Nettoyage : semaines terminées, séances supprimées ou sans structure
            for sid in list(etat):
                if sid in voulues:
                    continue
                f = self.fiches.get("seance", sid)
                fini = f and f.get("date") and f["date"] < monday.isoformat()
                if f and f.get("structure") and not fini:
                    continue  # au-delà de l'horizon : on la garde pour plus tard
                wid = etat[sid].get("workout_id")
                if wid:
                    try:
                        g.delete_workout(wid)
                    except Exception as exc:  # noqa: BLE001
                        LOG.info("Suppression de l'entraînement %s : %s", wid, exc)
                del etat[sid]
            self.state["derniere_synchro"] = _now()
            self.state["erreur"] = None
            self._save()
            return {"connecte": True, "envoyees": len(voulues) - erreurs, "erreurs": erreurs}

    def fail(self, exc: Exception) -> None:
        with self.lock:
            self.state["erreur"] = str(exc)[:300]
            if "auth" in str(exc).lower() or "401" in str(exc):
                self._client = None
            self._save()

    def statut(self) -> dict[str, Any]:
        with self.lock:
            return {"connecte": self.connecte(), "mfa_en_attente": self._pending_mfa is not None,
                    "derniere_synchro": self.state.get("derniere_synchro"), "erreur": self.state.get("erreur"),
                    "seances": {k: {x: v.get(x) for x in ("etat_envoi", "erreur", "date", "maj")}
                                for k, v in self.state["seances"].items()}}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
