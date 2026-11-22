"""Outils MCP exposés au coach Claude (lecture seule)."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import compact, fit_tools
from .fiches import INTENSITES, SPORTS_SEANCE, TYPES, FicheStore, iso_week
from .garmin import GarminClient
from .montre import MontreSync, StructureError, build_workout
from .suivi import SuiviStore

RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
DELETE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PATH = re.compile(r"^/[A-Za-z0-9_\-/.]+$")

INSTRUCTIONS = (
    "Données d'entraînement Garmin Connect de l'athlète (lecture seule). "
    "Commencer par list_activities pour trouver l'activity_id, puis get_activity_laps pour les tours "
    "(allure, FC, cadence), get_activity_streams pour le détail point par point, "
    "get_wellness pour la récupération (sommeil, VFC, FC repos). "
    "Les fiches de coaching (résumé de la semaine, séances, analyses, plan) se publient avec "
    "publish_fiche : elles s'affichent dans le panneau « Coach » de Home Assistant que l'athlète "
    "consulte sur son téléphone. Écrire en Markdown, court et lisible sur mobile. "
    "Pour les séances, renseigner sport et intensite (utilisés pour le suivi et les alertes d'enchaînement). "
    "Le panneau associe automatiquement les activités Garmin aux séances prévues (même un autre jour de la "
    "semaine), recueille le ressenti de l'athlète et lui permet de déplacer une séance dans la semaine : "
    "tout cela arrive dans une file d'événements (list_evenements) à traiter lors du point du soir, "
    "avec get_suivi_semaine pour la vue d'ensemble, puis marquer_evenements_traites."
)


def _d(value: str | None, default: date) -> str:
    if value is None or value == "":
        return default.isoformat()
    if not _DATE.match(value):
        raise ToolError(f"Date attendue au format AAAA-MM-JJ, reçu {value!r}")
    return value


_r = compact._r


def _fmt_s(seconds: Any) -> str | None:
    return fit_tools._fmt_s(seconds) if isinstance(seconds, (int, float)) else None


def compact_activity(a: dict[str, Any]) -> dict[str, Any]:
    sport = (a.get("activityType") or {}).get("typeKey")
    speed = a.get("averageSpeed")
    out = {
        "activity_id": a.get("activityId"),
        "nom": a.get("activityName"),
        "date_locale": a.get("startTimeLocal"),
        "sport": sport,
        "distance_km": round(a["distance"] / 1000, 2) if isinstance(a.get("distance"), (int, float)) else None,
        "duree": _fmt_s(a.get("duration")),
        "duree_mouvement": _fmt_s(a.get("movingDuration")),
        **fit_tools.pace_fields(speed if isinstance(speed, (int, float)) else None, sport),
        "fc_moy": _r(a.get("averageHR")),
        "fc_max": _r(a.get("maxHR")),
        "cadence_pas_min": _r(a.get("averageRunningCadenceInStepsPerMinute")),
        "d_plus_m": _r(a.get("elevationGain")),
        "puissance_moy_w": _r(a.get("avgPower")),
        "effet_aerobie": _r(a.get("aerobicTrainingEffect"), 1),
        "effet_anaerobie": _r(a.get("anaerobicTrainingEffect"), 1),
        "charge": _r(a.get("activityTrainingLoad")),
        "label_effet": a.get("trainingEffectLabel"),
        "vo2max": _r(a.get("vO2MaxValue")),
        "nb_tours": a.get("lapCount"),
        "description": a.get("description"),
    }
    return {k: v for k, v in out.items() if v not in (None, "")}


def build_server(garmin: GarminClient, store: FicheStore, suivi: SuiviStore, cache: Any = None,
                 montre: MontreSync | None = None) -> MCPServer:
    mcp = MCPServer(name="garmin-coach", instructions=INSTRUCTIONS, version="0.4.0")

    def kick() -> None:
        if montre:
            montre.kick()

    @mcp.tool(annotations=RO)
    def list_activities(start_date: str | None = None, end_date: str | None = None,
                        activity_type: str | None = None, limit: int = 30, start: int = 0) -> dict[str, Any]:
        """Liste les activités Garmin (résumé compact), des plus récentes aux plus anciennes.

        Dates au format AAAA-MM-JJ (par défaut : 14 derniers jours). activity_type : clé Garmin
        (ex. running, trail_running, lap_swimming, open_water_swimming, swimrun, strength_training) ;
        voir list_activity_types. limit max 100.
        """
        today = date.today()
        data = garmin.search_activities(
            start=max(0, start), limit=max(1, min(limit, 100)),
            start_date=_d(start_date, today - timedelta(days=14)), end_date=_d(end_date, today),
            activity_type=activity_type or None,
        )
        items = data if isinstance(data, list) else (data or {}).get("activityList", data)
        if not isinstance(items, list):
            return {"brut": data}
        return {"nombre": len(items), "activites": [compact_activity(a) for a in items if isinstance(a, dict)]}

    @mcp.tool(annotations=RO)
    def get_activity_summary(activity_id: int) -> dict[str, Any]:
        """Détail complet d'une activité tel que fourni par Garmin (JSON brut)."""
        return {"activite": garmin.activity(activity_id)}

    @mcp.tool(annotations=RO)
    def get_activity_laps(activity_id: int) -> dict[str, Any]:
        """Sessions et tours décodés depuis le fichier .fit : durée, distance, allure (min/km et /100 m
        en natation), FC moyenne/max, cadence (pas/min en course), D+, puissance, étape de séance.
        En swimrun, chaque tour indique son sport (course ou natation)."""
        return fit_tools.laps(garmin.activity_fit(activity_id))

    @mcp.tool(annotations=RO)
    def get_activity_streams(activity_id: int, interval_s: int = 10,
                             start_s: int | None = None, end_s: int | None = None) -> str:
        """Données point par point (CSV) : temps écoulé, distance, FC, allure, cadence, altitude, puissance.
        interval_s = pas d'échantillonnage en secondes ; start_s/end_s pour zoomer sur un passage."""
        return fit_tools.streams(garmin.activity_fit(activity_id), interval_s, start_s, end_s)

    @mcp.tool(annotations=RO)
    def get_wellness(start_date: str | None = None, end_date: str | None = None,
                     brut: bool = False) -> dict[str, Any]:
        """Récupération jour par jour (7 derniers jours par défaut), en résumé compact :
        sommeil (durée, phases, score), VFC (nuit, moyenne 7 j, zone normale, statut),
        journée (FC de repos, stress, Body Battery, pas, minutes intenses).
        brut=True renvoie les réponses Garmin complètes (très volumineuses)."""
        today = date.today()
        s, e = _d(start_date, today - timedelta(days=7)), _d(end_date, today)
        out: dict[str, Any] = {}
        sources = {
            "sommeil": ("/sleep-service/sleep/dailySleepSummariesByDate", {"startDate": s, "endDate": e}, compact.sleep),
            "vfc": (f"/hrv-service/hrv/daily/{s}/{e}", None, compact.hrv),
            "journees": ("/usersummary-service/v2/dailysummaries", {"startDate": s, "endDate": e}, compact.daily),
        }
        for key, (path, params, fmt) in sources.items():
            try:
                data = garmin.get(path, params)
                out[key] = data if brut else fmt(data)
            except Exception as exc:  # noqa: BLE001 - une source en échec ne bloque pas les autres
                out[key] = {"erreur": str(exc)}
        return out

    @mcp.tool(annotations=RO)
    def get_fitness_metrics(start_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
        """VO2max quotidienne (30 derniers jours par défaut) et zones de fréquence cardiaque."""
        today = date.today()
        s, e = _d(start_date, today - timedelta(days=30)), _d(end_date, today)
        out: dict[str, Any] = {}
        for key, path in {"vo2max": f"/metrics-service/metrics/maxmet/daily/{s}/{e}",
                          "zones_fc": "/biometric-service/heartRateZones/v2"}.items():
            try:
                out[key] = garmin.get(path)
            except Exception as exc:  # noqa: BLE001
                out[key] = {"erreur": str(exc)}
        return out

    @mcp.tool(annotations=RO)
    def list_activity_types() -> dict[str, Any]:
        """Types et sous-types d'activité Garmin (clés utilisables dans list_activities)."""
        return {"types": garmin.get("/activity-service/activity/activityTypes/simple")}

    @mcp.tool(annotations=RO)
    def garmin_get(path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        """Requête GET brute sur l'API personnelle Garmin (exploration d'un endpoint sans outil dédié).
        Ex. path='/hrv-service/hrv/2026-11-20'."""
        if not _PATH.match(path) or ".." in path:
            raise ToolError("Chemin invalide : doit commencer par / et ne contenir que lettres, chiffres, -, _, ., /")
        return {"reponse": garmin.get(path, params)}

    # ---- Fiches du panneau Coach ------------------------------------------------
    @mcp.tool(annotations=WRITE)
    def publish_fiche(type: str, titre: str, contenu: str, resume: str | None = None,
                      date: str | None = None, semaine: str | None = None, id: str | None = None,
                      sport: str | None = None, intensite: str | None = None,
                      structure: dict[str, Any] | None = None) -> dict[str, Any]:
        """Crée ou met à jour une fiche affichée dans le panneau « Coach » de Home Assistant.

        type : semaine (résumé hebdo, affiché en page d'accueil), seance (séance prévue ou réalisée),
        analyse (analyse ponctuelle : course, test, bilan santé), plan (plan d'entraînement).
        contenu : Markdown (titres ##, listes, **gras**, tableaux). Viser court et lisible sur mobile.
        resume : une ou deux phrases affichées en tête de fiche.
        date : AAAA-MM-JJ (jour de la séance / de l'analyse). semaine : AAAA-Wss (semaine ISO, lundi-dimanche).
        Pour type=semaine, une seule fiche par semaine (l'id vaut la semaine) : republier la met à jour.
        id : pour mettre à jour une fiche existante d'un autre type (voir list_fiches).
        Séances uniquement : sport (course, natation, swimrun, renfo, velo) et intensite (facile, dure,
        longue, force = renfo jambes lourd, renfo = renfo léger/haut du corps, technique). Sans eux, ils
        sont devinés d'après le titre. Écrire la cible de FC sous la forme « FC ≤ 150 » dans le résumé
        pour que le panneau vérifie la séance réalisée.

        structure (séances course, natation, renfo, crossfit) : entraînement envoyé automatiquement dans
        le calendrier Garmin de l'athlète (et sur sa montre) au jour de la séance. Format :
        {"sport": "course"|"natation"|"renfo"|"crossfit", "piscine_m": 25 (natation),
         "etapes": [ étape | {"type": "repeter", "fois": 5, "etapes": [...], "sans_dernier_repos": true} ]}
        étape = {"type": echauffement|effort|recup|repos|retour|autre,
                 fin : duree_min / duree_s / distance_m / reps / "fin": "lap",
                 "cible": {"fc_max": 150} | {"fc": [140, 150]} | {"allure": ["4:20", "4:10"]} (course),
                 "nage": libre|dos|brasse|papillon|4n|educatif|mixte, "materiel": pull|plaquettes|palmes|
                 planche|tuba (natation), "exercice": nom du catalogue Garmin (voir chercher_exercice),
                 "charge_kg", "note": consigne affichée sur la montre}.
        Préférences de l'athlète appliquées automatiquement : course → l'échauffement se termine par LAP
        (donner sa durée prévue, il lance les blocs quand il est à un endroit sûr) puis tout est automatique ;
        natation → nages à la distance, repos terminés par LAP (donner la durée indicative) ; renfo →
        séries en reps (LAP quand c'est fait) et repos chronométrés ; crossfit → blocs chronométrés
        (AMRAP = un effort en durée, EMOM = repeter de 60 s, for time = fin lap).
        structure={} retire l'entraînement de la montre."""
        if structure:
            try:
                build_workout(titre, structure, resume)
            except StructureError as exc:
                raise ToolError(f"structure invalide : {exc}") from exc
        out = store.save(type_=type, titre=titre, contenu=contenu, resume=resume, date_=date, semaine=semaine,
                         fiche_id=id, sport=sport, intensite=intensite, structure=structure)
        if type == "seance":
            kick()
            if structure:
                out["montre"] = "envoi en cours vers le calendrier Garmin (voir etat_montre)"
        return out

    @mcp.tool(annotations=RO)
    def list_fiches(type: str | None = None, semaine: str | None = None) -> dict[str, Any]:
        """Liste les fiches du panneau Coach (sans leur contenu), les plus récentes d'abord.
        Filtres facultatifs : type (semaine, seance, analyse, plan) et semaine (AAAA-Wss)."""
        items = store.list(type, semaine)
        return {"nombre": len(items), "fiches": [
            {k: f.get(k) for k in ("id", "type", "titre", "date", "semaine", "resume", "mis_a_jour")}
            for f in items]}

    @mcp.tool(annotations=RO)
    def get_fiche(type: str, id: str) -> dict[str, Any]:
        """Contenu complet d'une fiche du panneau Coach."""
        f = store.get(type, id)
        if f is None:
            raise ToolError(f"Aucune fiche {type}/{id}")
        return f

    @mcp.tool(annotations=DELETE)
    def delete_fiche(type: str, id: str) -> dict[str, Any]:
        """Supprime une fiche du panneau Coach (définitif)."""
        if not store.delete(type, id):
            raise ToolError(f"Aucune fiche {type}/{id}")
        return {"supprimee": f"{type}/{id}"}

    # ---- Suivi : séances réalisées, ressenti, déplacements -----------------------
    def _activites(week: str) -> list[dict[str, Any]]:
        from .panel import TTLCache, week_activities  # import tardif (dépendance circulaire)
        nonlocal cache
        cache = cache or TTLCache()
        return week_activities(garmin, cache, week)[1]

    @mcp.tool(annotations=RO)
    def get_suivi_semaine(semaine: str | None = None) -> dict[str, Any]:
        """Vue de suivi d'une semaine (AAAA-Wss, défaut : semaine en cours), synchronisée avec Garmin :
        chaque séance prévue avec son statut (realisee, manquee, aujourdhui, prevue), le jour d'origine si
        elle a été déplacée, l'activité associée (même faite un autre jour), le ressenti de l'athlète
        (effort 1-10, douleurs, note) ; les activités hors plan ; les alertes d'enchaînement."""
        week = semaine or iso_week(date.today())
        acts = _activites(week)
        suivi.sync(week, acts)
        v = suivi.vue(week, acts)
        for item in v["seances"]:
            f = store.get("seance", item["id"]) or {}
            item["titre"], item["resume"] = f.get("titre"), f.get("resume")
            a = item.get("activite")
            if a:
                item["activite"] = {k: a.get(k) for k in ("activity_id", "nom", "date_locale", "sport", "distance_km",
                                                          "duree", "allure_min_km", "allure_100m", "fc_moy", "fc_max",
                                                          "effet_aerobie", "charge", "label_effet") if a.get(k) is not None}
        for h in v["hors_plan"]:
            a = h["activite"]
            h["activite"] = {k: a.get(k) for k in ("activity_id", "nom", "date_locale", "sport", "distance_km", "duree",
                                                   "allure_min_km", "allure_100m", "fc_moy", "charge", "label_effet")
                             if a.get(k) is not None}
        return {"semaine": week, **v}

    @mcp.tool(annotations=RO)
    def list_evenements(tous: bool = False, limit: int = 100) -> dict[str, Any]:
        """File des événements remontés par le panneau Coach, non traités par défaut (tous=True pour
        l'historique) : realisee (séance faite, details.autre_jour si faite un autre jour), hors_plan,
        manquee, ressenti, deplacement (avec les alertes vues par l'athlète), association, reassociee.
        À traiter lors du point du soir, puis marquer_evenements_traites."""
        evs = suivi.evenements(non_traites=not tous, limit=max(1, min(limit, 400)))
        return {"nombre": len(evs), "evenements": [{k: v for k, v in e.items() if k != "cle"} for e in evs]}

    @mcp.tool(annotations=WRITE)
    def marquer_evenements_traites(ids: list[str] | None = None, tous: bool = False) -> dict[str, Any]:
        """Marque des événements comme traités (ids de list_evenements), ou tous les non traités avec tous=True."""
        if not ids and not tous:
            raise ToolError("Donner des ids ou tous=True")
        return {"traites": suivi.marquer_traites(None if tous else ids)}

    @mcp.tool(annotations=WRITE)
    def modifier_seance(id: str, date: str | None = None, sport: str | None = None, intensite: str | None = None,
                        activity_id: str | None = None, hors_plan_activity_id: str | None = None) -> dict[str, Any]:
        """Ajuste une séance prévue sans réécrire son contenu.
        date : la déplacer (n'importe quelle semaine, contrairement au panneau limité à la semaine).
        sport / intensite : corriger son classement (voir publish_fiche).
        activity_id : l'associer manuellement à une activité Garmin (corrige l'association automatique).
        hors_plan_activity_id : déclarer une activité « hors plan » (la détacher de toute séance)."""
        out: dict[str, Any] = {"id": id}
        if sport and sport not in SPORTS_SEANCE:
            raise ToolError(f"sport doit être l'un de : {', '.join(SPORTS_SEANCE)}")
        if intensite and intensite not in INTENSITES:
            raise ToolError(f"intensite doit être l'une de : {', '.join(INTENSITES)}")
        if store.get("seance", id) is None:
            raise ToolError(f"Aucune séance {id}")
        if sport or intensite:
            store.patch("seance", id, sport=sport, intensite=intensite)
        try:
            if date:
                if not _DATE.match(date):
                    raise ToolError("date attendue au format AAAA-MM-JJ")
                out["deplacement"] = suivi.deplacer(id, date, source="coach", meme_semaine=False)
            if activity_id:
                out["association"] = suivi.associer(activity_id, id, source="coach")
            if hors_plan_activity_id:
                out["hors_plan"] = suivi.associer(hors_plan_activity_id, None, source="coach")
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        kick()
        return out

    # ---- Montre : calendrier Garmin Connect ----------------------------------------
    @mcp.tool(annotations=RO)
    def etat_montre() -> dict[str, Any]:
        """État de l'envoi des séances vers la montre : connexion Garmin Connect, dernière synchro, et pour
        chaque séance l'état d'envoi (envoyee / erreur avec le message)."""
        if not montre:
            raise ToolError("Envoi montre indisponible")
        return montre.statut()

    @mcp.tool(annotations=WRITE)
    def synchroniser_montre() -> dict[str, Any]:
        """Synchronise tout de suite le calendrier Garmin avec les séances (sinon automatique toutes les
        30 min et après chaque modification). Séances de la semaine en cours et de la suivante."""
        if not montre:
            raise ToolError("Envoi montre indisponible")
        try:
            return montre.sync()
        except Exception as exc:  # noqa: BLE001
            montre.fail(exc)
            raise ToolError(f"Synchro en échec : {exc}") from exc

    @mcp.tool(annotations=RO)
    def chercher_exercice(terme: str) -> dict[str, Any]:
        """Cherche un exercice dans le catalogue Garmin (noms anglais, ex. « squat », « kettlebell swing »,
        « plank ») pour le champ « exercice » d'une structure renfo / crossfit."""
        from garminconnect import exercises

        res = exercises.find(terme)[:25]
        return {"nombre": len(res), "exercices": [e["name"] for e in res]}

    return mcp
