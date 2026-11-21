"""Mise en forme compacte des réponses Garmin (moins verbeuses, chiffres arrondis)."""

from __future__ import annotations

from typing import Any


def _r(v: Any, nd: int = 0) -> Any:
    """Arrondit les nombres (entier si nd=0), laisse le reste tel quel."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return v
    return int(round(v)) if nd == 0 else round(float(v), nd)


def _min(seconds: Any) -> Any:
    return int(round(seconds / 60)) if isinstance(seconds, (int, float)) else None


def _hm(seconds: Any) -> str | None:
    if not isinstance(seconds, (int, float)):
        return None
    m = int(round(seconds / 60))
    return f"{m // 60}h{m % 60:02d}"


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, "", {}, [])}


def _get(d: Any, *keys: str) -> Any:
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _as_list(data: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for k in keys:
            if isinstance(data.get(k), list):
                return [x for x in data[k] if isinstance(x, dict)]
    return []


def sleep(data: Any) -> list[dict[str, Any]] | Any:
    items = _as_list(data, "dailySleepDTOs", "data")
    if not items:
        return data
    out = []
    for s in items:
        scores = s.get("sleepScores") or {}
        out.append(_clean({
            "date": s.get("calendarDate"),
            "duree": _hm(s.get("sleepTimeSeconds")),
            "profond_min": _min(s.get("deepSleepSeconds")),
            "leger_min": _min(s.get("lightSleepSeconds")),
            "paradoxal_min": _min(s.get("remSleepSeconds")),
            "eveil_min": _min(s.get("awakeSleepSeconds")),
            "nb_reveils": s.get("awakeCount"),
            "score": _get(scores, "overall", "value"),
            "qualite": _get(scores, "overall", "qualifierKey"),
            "fc_moy": _r(s.get("avgHeartRate")),
            "stress_moy": _r(s.get("avgSleepStress")),
            "respiration_moy": _r(s.get("averageRespirationValue")),
            "besoin_min": _get(s, "sleepNeed", "actual"),
            "commentaire_garmin": s.get("sleepScoreFeedback"),
        }))
    return out


def hrv(data: Any) -> list[dict[str, Any]] | Any:
    items = _as_list(data, "hrvSummaries")
    if not items:
        return data
    out = []
    for h in items:
        base = h.get("baseline") or {}
        lo, hi = base.get("balancedLow"), base.get("balancedUpper")
        out.append(_clean({
            "date": h.get("calendarDate"),
            "nuit_ms": h.get("lastNightAvg"),
            "moy_7j_ms": h.get("weeklyAvg"),
            "pic_5min_ms": h.get("lastNight5MinHigh"),
            "zone_normale_ms": f"{lo}-{hi}" if lo is not None and hi is not None else None,
            "statut": h.get("status"),
        }))
    return out


def daily(data: Any) -> list[dict[str, Any]] | Any:
    items = _as_list(data, "data")
    if not items:
        return data
    out = []
    for d in items:
        hr, st, bb = d.get("heartRate") or {}, d.get("stress") or {}, d.get("bodyBattery") or {}
        im = d.get("intensityMinutes") or {}
        out.append(_clean({
            "date": d.get("calendarDate"),
            "fc_repos": hr.get("restingValue"),
            "fc_min": hr.get("minValue"),
            "fc_max": hr.get("maxValue"),
            "pas": _get(d, "movement", "steps", "value"),
            "stress_moy": st.get("avgLevel"),
            "stress_eleve_min": _min((st.get("highStressDurationInMillis") or 0) / 1000) if st else None,
            "body_battery_min": bb.get("minValue"),
            "body_battery_max": bb.get("maxValue"),
            "body_battery_recharge": bb.get("chargedValue"),
            "body_battery_depense": bb.get("drainedValue"),
            "minutes_intenses": (im.get("moderate") or 0) + 2 * (im.get("vigorous") or 0) if im else None,
            "calories_actives": _get(d, "calories", "burnedActive"),
        }))
    return out
