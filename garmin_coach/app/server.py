"""Outils MCP exposés au coach Claude (lecture seule)."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import fit_tools
from .garmin import GarminClient

RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PATH = re.compile(r"^/[A-Za-z0-9_\-/.]+$")

INSTRUCTIONS = (
    "Données d'entraînement Garmin Connect de l'athlète (lecture seule). "
    "Commencer par list_activities pour trouver l'activity_id, puis get_activity_laps pour les tours "
    "(allure, FC, cadence), get_activity_streams pour le détail point par point, "
    "get_wellness pour la récupération (sommeil, VFC, FC repos)."
)


def _d(value: str | None, default: date) -> str:
    if value is None or value == "":
        return default.isoformat()
    if not _DATE.match(value):
        raise ToolError(f"Date attendue au format AAAA-MM-JJ, reçu {value!r}")
    return value


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
        "fc_moy": a.get("averageHR"),
        "fc_max": a.get("maxHR"),
        "cadence_pas_min": a.get("averageRunningCadenceInStepsPerMinute"),
        "d_plus_m": a.get("elevationGain"),
        "puissance_moy_w": a.get("avgPower"),
        "effet_aerobie": a.get("aerobicTrainingEffect"),
        "effet_anaerobie": a.get("anaerobicTrainingEffect"),
        "charge": a.get("activityTrainingLoad"),
        "label_effet": a.get("trainingEffectLabel"),
        "vo2max": a.get("vO2MaxValue"),
        "nb_tours": a.get("lapCount"),
        "description": a.get("description"),
    }
    return {k: v for k, v in out.items() if v not in (None, "")}


def build_server(garmin: GarminClient) -> MCPServer:
    mcp = MCPServer(name="garmin-coach", instructions=INSTRUCTIONS, version="0.1.0")

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
    def get_wellness(start_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
        """Récupération sur une période (7 derniers jours par défaut) : résumés de sommeil, VFC (HRV),
        résumés quotidiens (FC de repos, stress, Body Battery)."""
        today = date.today()
        s, e = _d(start_date, today - timedelta(days=7)), _d(end_date, today)
        out: dict[str, Any] = {}
        sources = {
            "sommeil": ("/sleep-service/sleep/dailySleepSummariesByDate", {"startDate": s, "endDate": e}),
            "vfc": (f"/hrv-service/hrv/daily/{s}/{e}", None),
            "resumes_quotidiens": ("/usersummary-service/v2/dailysummaries", {"startDate": s, "endDate": e}),
        }
        for key, (path, params) in sources.items():
            try:
                out[key] = garmin.get(path, params)
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

    return mcp
