"""Panneau « Coach » servi via l'Ingress de Home Assistant : semaine, séances, suivi et ressenti."""

from __future__ import annotations

import ipaddress
import logging
import os
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

import markdown as md
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route

from . import compact
from .fiches import TYPES, FicheStore, iso_week, week_bounds
from .garmin import GarminClient
from .server import compact_activity
from .suivi import DOULEURS, SuiviStore

LOG = logging.getLogger("garmin_coach.panel")
STATIC = Path(__file__).parent / "static"
# Seul le proxy Ingress du Supervisor a le droit de parler au panneau
ALLOWED = [ipaddress.ip_network(n) for n in os.environ.get("PANEL_ALLOWED", "172.30.32.2/32").split(",")]

SPORTS = {
    "course": ("Course", ("running", "trail_running", "treadmill_running", "track_running", "street_running",
                          "indoor_running", "virtual_run", "ultra_run", "obstacle_run")),
    "natation": ("Natation", ("lap_swimming", "open_water_swimming", "swimming")),
    "swimrun": ("Swimrun", ("swimrun", "multi_sport")),
    "renfo": ("Renfo / CrossFit", ("strength_training", "hiit", "indoor_cardio", "fitness_equipment",
                                   "cardio_training", "crossfit", "pilates", "yoga")),
    "velo": ("Vélo", ("cycling", "road_biking", "mountain_biking", "gravel_cycling", "indoor_cycling",
                      "virtual_ride")),
}


def sport_group(type_key: str | None) -> str:
    for key, (_, keys) in SPORTS.items():
        if type_key in keys:
            return key
    if type_key and "swim" in type_key:
        return "natation"
    if type_key and "run" in type_key:
        return "course"
    return "autre"


class TTLCache:
    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str, ttl: float, fn: Callable[[], Any]) -> Any:
        now = time.monotonic()
        with self._lock:
            hit = self._data.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        value = fn()
        with self._lock:
            self._data[key] = (now, value)
        return value


def render_md(text: str) -> str:
    # Pas de HTML brut dans les fiches : on l'échappe avant la conversion Markdown
    safe = (text or "").replace("&", "&amp;").replace("<", "&lt;")
    return md.markdown(safe, extensions=["tables", "sane_lists", "nl2br"])


def fiche_out(f: dict[str, Any], with_html: bool = True) -> dict[str, Any]:
    out = {k: f.get(k) for k in ("id", "type", "titre", "resume", "date", "semaine", "mis_a_jour")}
    out["type_libelle"] = TYPES.get(f.get("type", ""), "")
    if with_html:
        out["html"] = render_md(f.get("contenu", ""))
    return out


def _sec(a: dict[str, Any]) -> float:
    d = a.get("duration")
    return float(d) if isinstance(d, (int, float)) else 0.0


def week_activities(garmin: GarminClient, cache: "TTLCache", week: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Activités brutes et compactes (avec groupe de sport et durée en s) d'une semaine ISO."""
    start, end = week_bounds(week)
    ttl = 300 if start <= date.today() <= end else 6 * 3600

    def acts() -> list[dict[str, Any]]:
        data = garmin.search_activities(0, 100, start.isoformat(), end.isoformat(), None)
        items = data if isinstance(data, list) else (data or {}).get("activityList", [])
        return [a for a in items if isinstance(a, dict)]

    raw = cache.get(f"acts:{week}", ttl, acts)
    out = []
    for a in sorted(raw, key=lambda a: a.get("startTimeLocal") or ""):
        c = compact_activity(a)
        c["groupe"] = sport_group(c.get("sport"))
        c["duree_s"] = _sec(a)
        out.append(c)
    return raw, out


def build_panel(garmin: GarminClient, store: FicheStore, suivi: SuiviStore, cache: "TTLCache | None" = None) -> Starlette:
    cache = cache or TTLCache()

    def week_data(week: str) -> dict[str, Any]:
        start, end = week_bounds(week)
        current = start <= date.today() <= end
        ttl = 300 if current else 6 * 3600
        errors: list[str] = []

        def forme() -> dict[str, Any]:
            s, e = start.isoformat(), min(end, date.today()).isoformat()
            out: dict[str, Any] = {}
            for key, path, params, fmt in (
                ("sommeil", "/sleep-service/sleep/dailySleepSummariesByDate", {"startDate": s, "endDate": e}, compact.sleep),
                ("vfc", f"/hrv-service/hrv/daily/{s}/{e}", None, compact.hrv),
                ("journees", "/usersummary-service/v2/dailysummaries", {"startDate": s, "endDate": e}, compact.daily),
            ):
                try:
                    out[key] = fmt(garmin.get(path, params))
                except Exception as exc:  # noqa: BLE001
                    out[key] = []
                    errors.append(f"{key} : {exc}")
            return out

        try:
            raw, activites = week_activities(garmin, cache, week)
            ok = True
        except Exception as exc:  # noqa: BLE001
            raw, activites, ok = [], [], False
            errors.append(f"activités : {exc}")
        f = cache.get(f"forme:{week}", ttl, forme) if start <= date.today() else {}

        totaux = {}
        for a, c in zip(sorted(raw, key=lambda a: a.get("startTimeLocal") or ""), activites):
            g = c["groupe"]
            t = totaux.setdefault(g, {"libelle": SPORTS.get(g, ("Autre",))[0], "seances": 0, "duree_s": 0.0,
                                      "distance_km": 0.0, "charge": 0})
            t["seances"] += 1
            t["duree_s"] += _sec(a)
            t["distance_km"] += (a.get("distance") or 0) / 1000
            t["charge"] += int(round(a.get("activityTrainingLoad") or 0))
        if ok:
            suivi.sync(week, activites)
        planning = suivi.vue(week, activites)
        fiches_seances = {x["id"]: fiche_out(x) for x in store.list("seance", week)}
        for item in planning["seances"]:
            item["fiche"] = fiches_seances.get(item["id"])

        jours = []
        by = {k: {x.get("date"): x for x in (f.get(k) or []) if isinstance(x, dict)} for k in ("sommeil", "vfc", "journees")}
        for i in range(7):
            d = (start + timedelta(days=i)).isoformat()
            s, v, j = by["sommeil"].get(d, {}), by["vfc"].get(d, {}), by["journees"].get(d, {})
            jours.append({
                "date": d,
                "vfc": v.get("nuit_ms"), "vfc_zone": v.get("zone_normale_ms"), "vfc_statut": v.get("statut"),
                "sommeil": s.get("duree"), "sommeil_score": s.get("score"),
                "fc_repos": j.get("fc_repos"), "bb_max": j.get("body_battery_max"),
                "stress": j.get("stress_moy"),
            })

        fiche = store.get("semaine", week.lower())
        return {
            "semaine": week, "debut": start.isoformat(), "fin": end.isoformat(), "courante": current,
            "precedente": iso_week(start - timedelta(days=7)), "suivante": iso_week(start + timedelta(days=7)),
            "fiche": fiche_out(fiche) if fiche else None,
            "planning": planning,
            "douleurs": list(DOULEURS),
            "activites": activites,
            "totaux": {
                "seances": len(activites),
                "duree_s": sum(_sec(a) for a in raw),
                "charge": sum(int(round(a.get("activityTrainingLoad") or 0)) for a in raw),
                "par_sport": sorted(totaux.values(), key=lambda t: -t["duree_s"]),
            },
            "jours": jours,
            "erreurs": errors,
        }

    async def index(request: Request):
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    def api_semaine(request: Request):
        week = request.query_params.get("w") or iso_week(date.today())
        try:
            week_bounds(week)
        except (ValueError, IndexError):
            return JSONResponse({"erreur": "semaine invalide"}, status_code=400)
        return JSONResponse(week_data(week))

    def api_fiches(request: Request):
        type_ = request.query_params.get("type")
        if type_ and type_ not in TYPES:
            return JSONResponse({"erreur": "type invalide"}, status_code=400)
        return JSONResponse({"fiches": [fiche_out(f) for f in store.list(type_)]})

    def api_semaines(request: Request):
        return JSONResponse({"fiches": [fiche_out(f, with_html=False) for f in store.list("semaine")]})

    async def _body(request: Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except ValueError:
            data = None
        return data if isinstance(data, dict) else {}

    def _err(exc: Exception) -> JSONResponse:
        return JSONResponse({"erreur": str(exc)}, status_code=400)

    async def api_deplacer(request: Request):
        b = await _body(request)
        try:
            res = suivi.deplacer(request.path_params["sid"], str(b.get("date", "")),
                                 simuler=bool(b.get("simuler")))
        except (ValueError, Exception) as exc:  # noqa: BLE001
            return _err(exc)
        return JSONResponse(res)

    async def api_ressenti(request: Request):
        b = await _body(request)
        try:
            rpe = b.get("rpe")
            res = suivi.ressenti(activity_id=b.get("activity_id"), seance_id=b.get("seance_id"),
                                 rpe=int(rpe) if rpe not in (None, "") else None,
                                 douleurs=[str(x) for x in (b.get("douleurs") or [])][:10], note=str(b.get("note") or ""))
        except (ValueError, TypeError) as exc:
            return _err(exc)
        return JSONResponse(res)

    async def api_associer(request: Request):
        b = await _body(request)
        try:
            res = suivi.associer(str(b.get("activity_id") or ""), b.get("seance_id") or None)
        except ValueError as exc:
            return _err(exc)
        return JSONResponse(res)

    async def api_fait(request: Request):
        b = await _body(request)
        try:
            suivi.fait_manuel(request.path_params["sid"], bool(b.get("fait", True)))
        except ValueError as exc:
            return _err(exc)
        return JSONResponse({"ok": True})

    app = Starlette(routes=[
        Route("/", index),
        Route("/api/semaine", api_semaine),
        Route("/api/fiches", api_fiches),
        Route("/api/semaines", api_semaines),
        Route("/api/seance/{sid}/deplacer", api_deplacer, methods=["POST"]),
        Route("/api/seance/{sid}/fait", api_fait, methods=["POST"]),
        Route("/api/ressenti", api_ressenti, methods=["POST"]),
        Route("/api/associer", api_associer, methods=["POST"]),
    ])
    return IngressOnly(app)


def sync_week(garmin: GarminClient, suivi: SuiviStore, cache: TTLCache, week: str) -> None:
    _, activites = week_activities(garmin, cache, week)
    suivi.sync(week, activites)


def sync_loop(garmin: GarminClient, suivi: SuiviStore, cache: TTLCache, interval_s: int = 1800) -> None:
    """Synchro en tâche de fond : associe les nouvelles activités aux séances et alimente la file du coach."""
    time.sleep(20)
    while True:
        today = date.today()
        weeks = [iso_week(today)]
        if today.weekday() <= 1:  # lundi/mardi : on finit aussi la semaine précédente
            weeks.insert(0, iso_week(today - timedelta(days=7)))
        for w in weeks:
            try:
                sync_week(garmin, suivi, cache, w)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Synchro du suivi (%s) en échec : %s", w, exc)
        time.sleep(interval_s)


class IngressOnly:
    """Refuse toute connexion qui ne vient pas du proxy Ingress de Home Assistant."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            ip = (scope.get("client") or ("0.0.0.0",))[0]
            try:
                ok = any(ipaddress.ip_address(ip) in n for n in ALLOWED)
            except ValueError:
                ok = False
            if not ok:
                LOG.warning("Panneau : accès refusé depuis %s", ip)
                return await PlainTextResponse("Forbidden", status_code=403)(scope, receive, send)
        return await self.app(scope, receive, send)

