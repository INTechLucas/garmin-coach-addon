"""Suivi des séances prévues : réalisation (association aux activités Garmin), ressenti,
déplacements, alertes d'enchaînement et file d'événements « à traiter » par le coach.

Tout l'état vit dans <dossier coach>/suivi.json (écriture atomique, verrou global) :
- seances   : {id_fiche: {date_initiale, deplacements[], fait_manuel, manquee_signalee}}
- activites : {activity_id: {seance_id, mode (auto|manuel|hors_plan), ressenti, resume}}
- evenements: [{id, cle, type, date, seance_id, activity_id, texte, details, cree_le, traite}]
- depuis    : horodatage de la première synchro (pas d'événement pour les activités plus anciennes)
"""

from __future__ import annotations

import json
import re
import secrets
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .fiches import INTENSITES, SPORTS_SEANCE, FicheStore, iso_week

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
DOULEURS = ("mollet", "achille", "pied", "genou", "hanche", "ischios", "dos", "epaule", "autre")
MAX_EVENTS = 400

# Mots-clés pour classer les séances publiées sans métadonnées
_KW_DURE = re.compile(r"seuil|fraction|vma|c[oô]tes?\b|fartlek|tempo|intervalle|\btest\b|vo2|qualit|sprint|wod", re.I)
_KW_LONGUE = re.compile(r"sortie longue|\bSL\b|long run|longue", re.I)
_KW_FORCE = re.compile(r"\bforce\b|jambes|squat|soulev|deadlift|renfo a\b", re.I)
_KW_TECH = re.compile(r"technique|[eé]ducatif", re.I)
_FC_CIBLE = re.compile(r"FC\s*(?:≤|<=|<|max\.?)\s*(\d{3})", re.I)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def jour(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{JOURS[d.weekday()]} {d.day}"


def infer_sport(f: dict[str, Any]) -> str:
    if f.get("sport") in SPORTS_SEANCE:
        return f["sport"]
    t = f"{f.get('titre', '')} {f.get('resume') or ''}".lower()
    if "swimrun" in t:
        return "swimrun"
    if re.search(r"renfo|muscu|crossfit|\bwod\b|hyrox|mobilit", t):
        return "renfo"
    if re.search(r"natation|\bnage\b|crawl|piscine|plaquettes|\bcss\b", t):
        return "natation"
    if re.search(r"\bforce\b|gainage", t):
        return "renfo"
    if re.search(r"v[eé]lo|home.?trainer", t):
        return "velo"
    return "course"


def infer_intensite(f: dict[str, Any], sport: str) -> str:
    if f.get("intensite") in INTENSITES:
        return f["intensite"]
    t = f"{f.get('titre', '')} {f.get('resume') or ''}"
    if sport == "renfo":
        return "force" if _KW_FORCE.search(t) else "renfo"
    if _KW_LONGUE.search(t):
        return "longue"
    if _KW_DURE.search(t):
        return "dure"
    if sport == "natation" and _KW_TECH.search(t):
        return "technique"
    return "facile"


def fc_cible(f: dict[str, Any]) -> int | None:
    m = _FC_CIBLE.search(f"{f.get('titre', '')} {f.get('resume') or ''}")
    return int(m.group(1)) if m else None


def activity_intensite(a: dict[str, Any]) -> str:
    label = a.get("label_effet") or ""
    if label in ("LACTATE_THRESHOLD", "VO2MAX", "ANAEROBIC_CAPACITY", "SPRINT", "TEMPO"):
        return "dure"
    if a.get("groupe") == "course" and (a.get("duree_s") or 0) >= 70 * 60:
        return "longue"
    return "facile"


def same_nature(act_int: str, seance_int: str) -> bool:
    """Intensité mesurée par Garmin vs intensité prévue (facile et technique sont interchangeables)."""
    doux = ("facile", "technique")
    return act_int == seance_int or (act_int in doux and seance_int in doux)


def compatible(seance_sport: str, groupe: str) -> bool:
    return seance_sport == groupe


class SuiviStore:
    def __init__(self, root: Path, fiches: FicheStore) -> None:
        self.path = root / "suivi.json"
        self.fiches = fiches
        self.lock = threading.RLock()
        self._state = self._load()

    # ---- persistance --------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        try:
            s = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            s = {}
        for k, v in (("seances", {}), ("activites", {}), ("evenements", []), ("depuis", None)):
            s.setdefault(k, v)
        return s

    def _save(self) -> None:
        self._state["evenements"] = self._state["evenements"][-MAX_EVENTS:]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    # ---- événements ---------------------------------------------------------
    def _event(self, cle: str, type_: str, texte: str, *, date_: str | None = None, seance_id: str | None = None,
               activity_id: str | None = None, details: dict[str, Any] | None = None, remplace: bool = False) -> None:
        evs = self._state["evenements"]
        existing = next((e for e in evs if e["cle"] == cle), None)
        if existing:
            if not remplace:
                return
            if existing["traite"]:
                existing = None
            else:
                evs.remove(existing)
        evs.append({
            "id": secrets.token_hex(4), "cle": cle, "type": type_, "date": date_, "seance_id": seance_id,
            "activity_id": activity_id, "texte": texte, "details": details or {}, "cree_le": now_iso(),
            "traite": False,
        })

    def _drop_pending(self, prefix: str) -> None:
        self._state["evenements"] = [e for e in self._state["evenements"]
                                     if e["traite"] or not e["cle"].startswith(prefix)]

    def evenements(self, non_traites: bool = True, limit: int = 100) -> list[dict[str, Any]]:
        with self.lock:
            evs = [e for e in self._state["evenements"] if not (non_traites and e["traite"])]
        return evs[-limit:]

    def marquer_traites(self, ids: list[str] | None) -> int:
        n = 0
        with self.lock:
            for e in self._state["evenements"]:
                if not e["traite"] and (ids is None or e["id"] in ids):
                    e["traite"], e["traite_le"] = True, now_iso()
                    n += 1
            self._save()
        return n

    def en_attente(self) -> int:
        with self.lock:
            return sum(1 for e in self._state["evenements"] if not e["traite"])

    # ---- séances ------------------------------------------------------------
    def seances(self, week: str) -> list[dict[str, Any]]:
        out = []
        for f in self.fiches.list("seance", week):
            if not f.get("date"):
                continue
            sport = infer_sport(f)
            out.append({"fiche": f, "id": f["id"], "date": f["date"], "sport": sport,
                        "intensite": infer_intensite(f, sport)})
        out.sort(key=lambda s: (s["date"], s["fiche"].get("cree_le") or ""))
        return out

    def _sinfo(self, sid: str) -> dict[str, Any]:
        return self._state["seances"].setdefault(sid, {})

    # ---- association activités <-> séances -----------------------------------
    def sync(self, week: str, activites: list[dict[str, Any]], today: date | None = None) -> None:
        """activites : activités compactes de la semaine (avec activity_id, groupe, date_locale, duree_s)."""
        today = today or date.today()
        with self.lock:
            if not self._state["depuis"]:
                self._state["depuis"] = now_iso()
            depuis = self._state["depuis"][:19].replace("T", " ")
            seances = self.seances(week)
            by_id = {s["id"]: s for s in seances}
            acts = {str(a["activity_id"]): a for a in activites if a.get("activity_id") is not None}
            amap = self._state["activites"]

            # 1. Liens verrouillés (manuels) encore valides
            taken: dict[str, str] = {}
            for aid, a in acts.items():
                info = amap.get(aid, {})
                if info.get("mode") == "manuel" and info.get("seance_id") in by_id:
                    taken[info["seance_id"]] = aid
            fixed = {aid for aid in acts if amap.get(aid, {}).get("mode") in ("manuel", "hors_plan")}
            for sid, s in by_id.items():
                if self._state["seances"].get(sid, {}).get("fait_manuel"):
                    taken.setdefault(sid, "manuel")

            # 2. Association automatique : meilleurs couples d'abord (même jour, même intensité)
            pairs = []
            for aid, a in acts.items():
                if aid in fixed or a.get("groupe") in (None, "autre"):
                    continue
                ad = date.fromisoformat(a["date_locale"][:10])
                for sid, s in by_id.items():
                    if sid in taken or not compatible(s["sport"], a["groupe"]):
                        continue
                    ecart = (date.fromisoformat(s["date"]) - ad).days
                    meme_nature = s["sport"] == "renfo" or same_nature(activity_intensite(a), s["intensite"])
                    if ecart != 0 and not meme_nature:
                        continue  # un autre jour : seulement si c'est le même type de séance
                    if ecart > 2:
                        continue  # séance faite en avance : au plus 2 jours avant la date prévue
                    score = abs(ecart) * 10 + (0 if meme_nature else 3)
                    pairs.append((score, a["date_locale"], aid, sid))
            pairs.sort()
            auto: dict[str, str] = {}
            used_s = set(taken)
            for _, _, aid, sid in pairs:
                if aid in auto or sid in used_s:
                    continue
                auto[aid] = sid
                used_s.add(sid)

            # 3. Mise à jour de l'état + événements
            for aid, a in acts.items():
                info = amap.setdefault(aid, {})
                info["resume"] = {k: a.get(k) for k in ("nom", "date_locale", "sport", "groupe", "distance_km", "duree",
                                                        "allure_min_km", "allure_100m", "fc_moy", "label_effet")}
                if info.get("mode") in ("manuel", "hors_plan"):
                    continue
                new_sid = auto.get(aid)
                old_sid = info.get("seance_id")
                info["mode"] = "auto"
                info["seance_id"] = new_sid
                recent = (a.get("date_locale") or "") >= depuis
                if not recent or a.get("groupe") in (None, "autre"):
                    continue
                adate = a["date_locale"][:10]
                if new_sid:
                    s = by_id[new_sid]
                    autre = s["date"] != adate
                    txt = f"{s['fiche']['titre']} réalisée" + (f" le {jour(adate)} (prévue {jour(s['date'])})" if autre else "")
                    self._event(f"realisee:{aid}:{new_sid}", "realisee", txt, date_=adate, seance_id=new_sid,
                                activity_id=aid, details={"autre_jour": autre})
                    if old_sid and old_sid != new_sid:
                        self._drop_pending(f"realisee:{aid}:")
                        self._event(f"reassociee:{aid}:{new_sid}", "reassociee",
                                    f"Activité du {jour(adate)} réassociée à « {s['fiche']['titre']} »",
                                    date_=adate, seance_id=new_sid, activity_id=aid)
                else:
                    if old_sid:
                        self._drop_pending(f"realisee:{aid}:")
                    self._event(f"hors_plan:{aid}", "hors_plan",
                                f"Activité hors plan le {jour(adate)} : {a.get('nom') or a.get('sport')}",
                                date_=adate, activity_id=aid)

            # 4. Séances manquées (jour passé, rien d'associé)
            linked = {i.get("seance_id") for aid, i in amap.items() if aid in acts and i.get("seance_id")}
            for sid, s in by_id.items():
                si = self._sinfo(sid)
                done = sid in linked or si.get("fait_manuel")
                if not done and date.fromisoformat(s["date"]) < today and not si.get("manquee_signalee") \
                        and (s["fiche"].get("cree_le") or "") <= f"{s['date']}T23:59":
                    si["manquee_signalee"] = True
                    self._event(f"manquee:{sid}", "manquee", f"{s['fiche']['titre']} non réalisée ({jour(s['date'])})",
                                date_=s["date"], seance_id=sid)
            self._save()

    def est_realisee(self, sid: str) -> bool:
        with self.lock:
            if self._state["seances"].get(sid, {}).get("fait_manuel"):
                return True
            return any(i.get("seance_id") == sid for i in self._state["activites"].values())

    # ---- vue de la semaine -----------------------------------------------------
    def vue(self, week: str, activites: list[dict[str, Any]], today: date | None = None) -> dict[str, Any]:
        today = today or date.today()
        with self.lock:
            seances = self.seances(week)
            amap = self._state["activites"]
            acts = {str(a["activity_id"]): a for a in activites if a.get("activity_id") is not None}
            par_seance = {i["seance_id"]: aid for aid, i in amap.items() if aid in acts and i.get("seance_id")}
            out = []
            for s in seances:
                si = self._state["seances"].get(s["id"], {})
                aid = par_seance.get(s["id"])
                a = acts.get(aid) if aid else None
                if a or si.get("fait_manuel"):
                    statut = "realisee"
                elif date.fromisoformat(s["date"]) < today:
                    statut = "manquee"
                elif s["date"] == today.isoformat():
                    statut = "aujourdhui"
                else:
                    statut = "prevue"
                item = {
                    "id": s["id"], "date": s["date"], "sport": s["sport"], "intensite": s["intensite"],
                    "statut": statut, "date_initiale": si.get("date_initiale"),
                    "fait_manuel": bool(si.get("fait_manuel")), "fc_cible": fc_cible(s["fiche"]),
                }
                if a:
                    item["activite"] = a
                    item["date_realisee"] = a["date_locale"][:10]
                    item["mode"] = amap[aid].get("mode")
                    item["ressenti"] = amap[aid].get("ressenti")
                elif si.get("fait_manuel"):
                    item["ressenti"] = si.get("ressenti")
                out.append(item)
            hors = []
            for aid, a in acts.items():
                info = amap.get(aid, {})
                if info.get("seance_id") and info["seance_id"] in {s["id"] for s in seances}:
                    continue
                if a.get("groupe") in (None, "autre") and not info.get("ressenti"):
                    continue
                hors.append({"activite": a, "ressenti": info.get("ressenti"), "mode": info.get("mode")})
            return {"seances": out, "hors_plan": hors, "alertes": alertes(seances), "en_attente": self.en_attente()}

    # ---- actions de l'athlète ---------------------------------------------------
    def deplacer(self, sid: str, new_date: str, *, source: str = "panneau", meme_semaine: bool = True,
                 simuler: bool = False) -> dict[str, Any]:
        with self.lock:
            f = self.fiches.get("seance", sid)
            if not f or not f.get("date"):
                raise ValueError("séance introuvable")
            old = f["date"]
            nd = date.fromisoformat(new_date)
            if meme_semaine and iso_week(nd) != f.get("semaine", iso_week(date.fromisoformat(old))):
                raise ValueError("déplacement possible seulement dans la même semaine (passe par le chat sinon)")
            week = iso_week(nd)
            seances = [dict(s) for s in self.seances(week)]
            sport = infer_sport(f)
            moved = {"fiche": f, "id": sid, "date": new_date, "sport": sport, "intensite": infer_intensite(f, sport)}
            seances = [s for s in seances if s["id"] != sid] + [moved]
            al = [x for x in alertes(seances) if sid in x["seances"]]
            if simuler or new_date == old:
                return {"alertes": al, "simulation": True}
            si = self._sinfo(sid)
            si.setdefault("date_initiale", old)
            si.setdefault("deplacements", []).append({"de": old, "vers": new_date, "le": now_iso(), "par": source})
            if si.get("date_initiale") == new_date:
                si.pop("date_initiale", None)
            si.pop("manquee_signalee", None)
            self.fiches.patch("seance", sid, date_=new_date, semaine=week)
            if source == "panneau":
                txt = f"« {f['titre']} » déplacée du {jour(old)} au {jour(new_date)}"
                if al:
                    txt += " — alerte : " + "; ".join(x["texte"] for x in al)
                self._event(f"deplacement:{sid}:{now_iso()}", "deplacement", txt, date_=new_date, seance_id=sid,
                            details={"de": old, "vers": new_date, "alertes": [x["texte"] for x in al]})
            self._save()
            return {"alertes": al, "date": new_date, "date_initiale": si.get("date_initiale")}

    def ressenti(self, *, activity_id: str | None, seance_id: str | None, rpe: int | None,
                 douleurs: list[str], note: str) -> dict[str, Any]:
        if rpe is not None and not 1 <= int(rpe) <= 10:
            raise ValueError("effort perçu entre 1 et 10")
        douleurs = [d for d in douleurs if d in DOULEURS]
        note = (note or "").strip()[:600]
        r = {"rpe": int(rpe) if rpe is not None else None, "douleurs": douleurs, "note": note, "le": now_iso()}
        with self.lock:
            if activity_id:
                info = self._state["activites"].get(str(activity_id))
                if info is None:
                    raise ValueError("activité inconnue")
                info["ressenti"] = r
                seance_id = seance_id or info.get("seance_id")
                nom = (info.get("resume") or {}).get("nom") or "activité"
                d = ((info.get("resume") or {}).get("date_locale") or "")[:10] or None
            else:
                si = self._sinfo(seance_id or "")
                f = self.fiches.get("seance", seance_id or "")
                if not f:
                    raise ValueError("séance introuvable")
                si["ressenti"] = r
                nom, d = f["titre"], f.get("date")
            parts = [f"effort {r['rpe']}/10" if r["rpe"] else None,
                     ("douleur : " + ", ".join(douleurs)) if douleurs else None, f"« {note} »" if note else None]
            self._event(f"ressenti:{activity_id or seance_id}", "ressenti",
                        f"Ressenti sur {nom} : " + (" · ".join(p for p in parts if p) or "rien à signaler"),
                        date_=d, seance_id=seance_id, activity_id=str(activity_id) if activity_id else None,
                        details=r, remplace=True)
            self._save()
        return r

    def associer(self, activity_id: str, seance_id: str | None, source: str = "panneau") -> dict[str, Any]:
        """Associe manuellement une activité à une séance (seance_id=None : hors plan ; 'auto' : rendre à l'automatique)."""
        with self.lock:
            info = self._state["activites"].setdefault(str(activity_id), {})
            if seance_id == "auto":
                info["mode"], info["seance_id"] = "auto", None
            elif seance_id:
                if not self.fiches.get("seance", seance_id):
                    raise ValueError("séance introuvable")
                for aid, i in self._state["activites"].items():  # une seule activité par séance
                    if aid != str(activity_id) and i.get("seance_id") == seance_id:
                        i["seance_id"], i["mode"] = None, "auto"
                info["mode"], info["seance_id"] = "manuel", seance_id
            else:
                info["mode"], info["seance_id"] = "hors_plan", None
            nom = (info.get("resume") or {}).get("nom") or "activité"
            if source == "panneau":
                cible = self.fiches.get("seance", seance_id)["titre"] if seance_id and seance_id != "auto" else "hors plan"
                self._event(f"association:{activity_id}:{now_iso()}", "association",
                            f"{nom} associée manuellement : {cible}", activity_id=str(activity_id), seance_id=seance_id)
            self._save()
        return {"activity_id": str(activity_id), "seance_id": info.get("seance_id"), "mode": info["mode"]}

    def fait_manuel(self, seance_id: str, fait: bool = True) -> None:
        with self.lock:
            f = self.fiches.get("seance", seance_id)
            if not f:
                raise ValueError("séance introuvable")
            si = self._sinfo(seance_id)
            si["fait_manuel"] = fait
            self._event(f"fait_manuel:{seance_id}:{fait}", "realisee" if fait else "annulation",
                        f"{f['titre']} marquée {'faite (sans montre)' if fait else 'non faite'}",
                        date_=f.get("date"), seance_id=seance_id, remplace=True)
            self._save()


def alertes(seances: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Enchaînements à éviter dans une semaine (ne bloquent rien, signalent seulement)."""
    by_day: dict[str, list[dict[str, Any]]] = {}
    for s in seances:
        by_day.setdefault(s["date"], []).append(s)
    out = []

    def add(texte: str, ids: list[str], niveau: str = "attention") -> None:
        out.append({"texte": texte, "seances": ids, "niveau": niveau})

    for d, items in sorted(by_day.items()):
        if len(items) >= 3:
            add(f"{len(items)} séances le {jour(d)} : grosse journée, garde au moins 4 h entre elles",
                [s["id"] for s in items])
        hard = [s for s in items if s["intensite"] == "dure"]
        if len(hard) >= 2:
            add(f"Deux séances dures le {jour(d)}", [s["id"] for s in hard])
        nxt = (date.fromisoformat(d) + timedelta(days=1)).isoformat()
        for a in items:
            for b in by_day.get(nxt, []):
                if a["intensite"] == "dure" and b["intensite"] == "dure":
                    add(f"Deux séances dures de suite ({jour(d)} puis {jour(nxt)}) : la seconde sera de moins bonne "
                        f"qualité et le risque de blessure monte", [a["id"], b["id"]])
                elif a["intensite"] == "longue" and b["intensite"] == "dure" and b["sport"] in ("course", "swimrun"):
                    add(f"Séance dure le lendemain de la sortie longue ({jour(nxt)}) : jambes encore chargées",
                        [a["id"], b["id"]])
                elif a["intensite"] == "force" and b["intensite"] in ("dure", "longue") and b["sport"] in ("course", "swimrun"):
                    add(f"Renfo jambes lourd la veille de « {b['fiche']['titre']} » : la qualité de course en pâtira",
                        [a["id"], b["id"]])
    return out
