"""Client minimal (lecture seule) pour l'API personnelle Garmin Connect.

Endpoints et authentification repris du client officiel de Garmin :
https://github.com/garmin/connect-personal-api-python
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

import requests
from mcp.server.mcpserver.exceptions import ToolError

BASE_URL = os.environ.get("GARMIN_PERSONAL_API_BASE_URL", "https://personal.connectapi.garmin.com").rstrip("/")
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
REFRESH_MARGIN_S = 3600  # on renouvelle le jeton s'il expire dans moins d'une heure

_LOG = logging.getLogger("garmin_coach.garmin")


class GarminError(ToolError):
    """Erreur dont le message est transmis tel quel à Claude."""


class GarminClient:
    def __init__(self, api_key: str, data_dir: Path, timeout: float = 30.0) -> None:
        if not api_key or not api_key.strip():
            raise GarminError("Clé API Garmin absente : renseignez 'garmin_api_key' dans la configuration de l'add-on.")
        self._api_key = api_key.strip()
        self._timeout = timeout
        self._session = requests.Session()
        self._lock = threading.Lock()
        self._token: str | None = None
        self._token_exp: float = 0.0
        self.fit_dir = data_dir / "fit"
        self.fit_dir.mkdir(parents=True, exist_ok=True)

    # ---- authentification -------------------------------------------------
    def _bearer(self, force: bool = False) -> str:
        with self._lock:
            if not force and self._token and self._token_exp - time.time() > REFRESH_MARGIN_S:
                return self._token
            _LOG.info("Demande d'un nouveau jeton d'accès Garmin")
            resp = self._session.post(
                BASE_URL + TOKEN_PATH,
                headers={"Accept": "*/*", "Authorization": f"ApiKey {self._api_key}"},
                timeout=self._timeout,
            )
            if not resp.ok:
                raise GarminError(
                    f"Échec d'obtention du jeton Garmin (HTTP {resp.status_code}). "
                    "Vérifiez que la clé API est valide et non expirée."
                )
            token = resp.text.strip()
            self._token, self._token_exp = token, _jwt_exp(token)
            return token

    # ---- requêtes ---------------------------------------------------------
    def _request(self, path: str, params: dict[str, Any] | None = None, accept: str = "application/json") -> requests.Response:
        for attempt in (0, 1):
            resp = self._session.get(
                BASE_URL + path,
                params=params,
                headers={"Accept": accept, "Authorization": f"Bearer {self._bearer(force=attempt == 1)}"},
                timeout=self._timeout,
            )
            if resp.status_code == 401 and attempt == 0:
                continue  # jeton révoqué : on en redemande un, une seule fois
            if resp.status_code == 429:
                raise GarminError("Garmin limite le nombre de requêtes (HTTP 429) ; réessayer plus tard.")
            if not resp.ok:
                raise GarminError(f"GET {path} : HTTP {resp.status_code} {resp.text[:300]}")
            return resp
        raise AssertionError("unreachable")

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = self._request(path, params)
        return resp.json() if resp.content else None

    # ---- jeux de données --------------------------------------------------
    def search_activities(self, start: int, limit: int, start_date: str | None, end_date: str | None,
                          activity_type: str | None) -> Any:
        params: dict[str, Any] = {"start": start, "limit": limit}
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date
        if activity_type:
            params["activityType"] = activity_type
        return self.get("/activitylist-service/activities/search/activities", params)

    def activity(self, activity_id: int) -> Any:
        return self.get(f"/activity-service/activity/{int(activity_id)}")

    def activity_fit(self, activity_id: int) -> Path:
        """Renvoie le chemin du .fit (téléchargé une fois puis archivé sur la box)."""
        activity_id = int(activity_id)
        if activity_id <= 0:
            raise GarminError("activity_id doit être un entier positif")
        path = self.fit_dir / f"{activity_id}.fit"
        if path.exists() and path.stat().st_size > 0:
            return path
        resp = self._request(
            f"/download-service/files/activity/{activity_id}",
            accept="application/zip, application/x-zip-compressed, application/octet-stream",
        )
        content = resp.content
        if content[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".fit")]
                if not names:
                    raise GarminError(f"Le fichier de l'activité {activity_id} ne contient pas de .fit ({zf.namelist()})")
                content = zf.read(names[0])
        tmp = path.with_suffix(".part")
        tmp.write_bytes(content)
        tmp.replace(path)
        return path


def _jwt_exp(token: str) -> float:
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return float(data["exp"])
    except Exception as exc:  # noqa: BLE001
        raise GarminError("Le jeton renvoyé par Garmin n'est pas un JWT valide") from exc
