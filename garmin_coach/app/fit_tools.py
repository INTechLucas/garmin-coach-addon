"""Décodage des fichiers .fit : sessions, tours, et séries point par point."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import fitdecode

# Sports où la cadence .fit est en foulées/min (un pied) : on la double en pas/min
_STEP_SPORTS = {"running", "walking", "hiking", "training", "swimrun"}


def _messages(path: Path) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    with fitdecode.FitReader(str(path)) as reader:
        for frame in reader:
            if isinstance(frame, fitdecode.FitDataMessage):
                out.append((frame.name, {f.name: f.value for f in frame.fields if f.value is not None}))
    return out


def _speed(m: dict[str, Any], prefix: str = "avg") -> float | None:
    v = m.get(f"enhanced_{prefix}_speed", m.get(f"{prefix}_speed"))
    return float(v) if isinstance(v, (int, float)) and v > 0 else None


def _fmt_s(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def pace_fields(speed: float | None, sport: str | None) -> dict[str, str]:
    if not speed:
        return {}
    out = {"allure_min_km": _fmt_s(1000 / speed)}
    if sport and "swim" in str(sport):
        out["allure_100m"] = _fmt_s(100 / speed)
    return out


def _cadence(m: dict[str, Any], sport: str | None) -> float | None:
    cad = m.get("avg_running_cadence", m.get("avg_cadence"))
    if not isinstance(cad, (int, float)):
        return None
    cad = cad + (m.get("avg_fractional_cadence") or 0)
    return round(cad * 2) if sport in _STEP_SPORTS else round(cad, 1)


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


def _summarize(m: dict[str, Any], default_sport: str | None) -> dict[str, Any]:
    sport = str(m.get("sport", default_sport) or "") or None
    speed = _speed(m)
    out: dict[str, Any] = {
        "sport": sport,
        "sous_sport": m.get("sub_sport"),
        "debut_utc": _iso(m.get("start_time")),
        "duree": _fmt_s(m.get("total_timer_time")),
        "duree_totale": _fmt_s(m.get("total_elapsed_time")),
        "distance_m": round(m["total_distance"], 1) if isinstance(m.get("total_distance"), (int, float)) else None,
        **pace_fields(speed, sport),
        "fc_moy": m.get("avg_heart_rate"),
        "fc_max": m.get("max_heart_rate"),
        "cadence": _cadence(m, sport),
        "d_plus_m": m.get("total_ascent"),
        "d_moins_m": m.get("total_descent"),
        "calories": m.get("total_calories"),
        "puissance_moy_w": m.get("avg_power"),
        "intensite": m.get("intensity"),
        "declencheur": m.get("lap_trigger"),
        "etape_seance": m.get("wkt_step_index"),
        "effet_aerobie": round(m["total_training_effect"], 1) if isinstance(m.get("total_training_effect"), (int, float)) else None,
        "effet_anaerobie": round(m["total_anaerobic_training_effect"], 1) if isinstance(m.get("total_anaerobic_training_effect"), (int, float)) else None,
    }
    return {k: v for k, v in out.items() if v is not None}


def laps(path: Path) -> dict[str, Any]:
    msgs = _messages(path)
    sport = next((str(m.get("sport")) for n, m in msgs if n == "sport" and m.get("sport")), None)
    sessions = [_summarize(m, sport) for n, m in msgs if n == "session"]
    session_sport = sessions[0].get("sport") if len(sessions) == 1 else sport
    tours = []
    for i, (n, m) in enumerate((x for x in msgs if x[0] == "lap"), start=1):
        tours.append({"tour": i, **_summarize(m, session_sport)})
    return {"heures": "UTC", "sessions": sessions, "tours": tours}


def streams(path: Path, interval_s: int = 10, start_s: int | None = None, end_s: int | None = None,
            max_rows: int = 2000) -> str:
    """Séries temporelles échantillonnées, au format CSV compact."""
    interval_s = max(1, int(interval_s))
    records = [m for n, m in _messages(path) if n == "record" and isinstance(m.get("timestamp"), datetime)]
    if not records:
        return "Aucun point enregistré dans ce fichier."
    t0 = records[0]["timestamp"]
    header = "t_s,dist_m,fc,allure_min_km,cadence_pas_min,alt_m,puissance_w"
    rows = [header]
    next_t = start_s or 0
    last_row = None
    for m in records:
        t = (m["timestamp"] - t0).total_seconds()
        if start_s is not None and t < start_s:
            continue
        if end_s is not None and t > end_s:
            break
        speed = m.get("enhanced_speed", m.get("speed"))
        cad = m.get("cadence")
        if isinstance(cad, (int, float)):
            cad = round((cad + (m.get("fractional_cadence") or 0)) * 2)
        alt = m.get("enhanced_altitude", m.get("altitude"))
        row = ",".join(
            "" if v is None else str(v)
            for v in (
                int(t),
                round(m["distance"]) if isinstance(m.get("distance"), (int, float)) else None,
                m.get("heart_rate"),
                _fmt_s(1000 / speed) if isinstance(speed, (int, float)) and speed > 0.3 else None,
                cad,
                round(alt, 1) if isinstance(alt, (int, float)) else None,
                m.get("power"),
            )
        )
        last_row = row
        if t >= next_t:
            rows.append(row)
            next_t = t + interval_s
            if len(rows) > max_rows:
                rows.append(f"# tronqué à {max_rows} lignes : augmenter interval_s ou réduire la plage")
                return "\n".join(rows)
    if last_row and rows[-1] != last_row:
        rows.append(last_row)
    return "\n".join(rows)
