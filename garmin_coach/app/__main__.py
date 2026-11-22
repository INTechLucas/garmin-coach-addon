"""Point d'entrée de l'add-on : lit la configuration et lance le serveur MCP en HTTPS."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import secrets
import shutil
import threading
from pathlib import Path

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings

from .fiches import FicheStore
from .garmin import GarminClient
from .panel import TTLCache, build_panel, sync_loop
from .server import build_server
from .montre import MontreSync
from .suivi import SuiviStore

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
OPTIONS_FILE = Path(os.environ.get("OPTIONS_FILE", DATA_DIR / "options.json"))
SSL_DIR = Path(os.environ.get("SSL_DIR", "/ssl"))
FIT_DIR = Path(os.environ.get("FIT_DIR", "/share/garmin-coach/fit"))
COACH_DIR = Path(os.environ.get("COACH_DIR", "/share/garmin-coach/coach"))
PANEL_PORT = int(os.environ.get("PANEL_PORT", "8099"))
# Réseau interne des add-ons HA : seul un proxy situé ici peut indiquer l'IP réelle du client
TRUSTED_PROXIES = ipaddress.ip_network("172.30.32.0/23")
PORT = int(os.environ.get("PORT", "8766"))

LOG = logging.getLogger("garmin_coach")


def load_access_token(options: dict) -> str:
    token = (options.get("access_token") or "").strip()
    if token:
        if len(token) < 32 or not token.replace("-", "").replace("_", "").isalnum():
            raise SystemExit("access_token doit faire au moins 32 caractères (lettres, chiffres, - ou _).")
        return token
    stored = DATA_DIR / "access_token"
    if stored.exists():
        return stored.read_text().strip()
    token = secrets.token_urlsafe(32)
    fd = os.open(stored, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    LOG.warning("Jeton d'accès généré automatiquement (affiché une seule fois) : %s", token)
    LOG.warning("Copiez-le dans l'option 'access_token' de l'add-on pour le conserver de façon visible.")
    return token


def migrate_fit_files(old_dir: Path, new_dir: Path) -> None:
    """Déplace les .fit de l'ancien emplacement privé (/data/fit, v0.1.0) vers /share."""
    if not old_dir.is_dir() or old_dir.resolve() == new_dir.resolve():
        return
    new_dir.mkdir(parents=True, exist_ok=True)
    moved = 0
    for f in old_dir.glob("*.fit"):
        target = new_dir / f.name
        if not target.exists():
            shutil.move(str(f), target)
            moved += 1
        else:
            f.unlink()
    try:
        old_dir.rmdir()
    except OSError:
        pass
    if moved:
        LOG.info("%d fichier(s) .fit déplacé(s) vers %s", moved, new_dir)


def client_ip(scope) -> str:
    """IP du client ; si la requête vient du proxy NGINX interne, on lit X-Forwarded-For."""
    ip = (scope.get("client") or ("?",))[0]
    try:
        if ipaddress.ip_address(ip) in TRUSTED_PROXIES:
            for name, value in scope.get("headers", []):
                if name == b"x-forwarded-for":
                    return value.decode("latin-1").split(",")[0].strip() + " (via proxy)"
    except ValueError:
        pass
    return ip


class Guard:
    """Ne laisse passer que les requêtes dont le premier segment d'URL est le jeton secret."""

    def __init__(self, app, token: str) -> None:
        self.app, self.token = app, token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        client = client_ip(scope)
        segment = scope.get("path", "").lstrip("/").split("/", 1)[0]
        if scope["type"] != "http" or not hmac.compare_digest(segment.encode(), self.token):
            LOG.warning("Requête refusée depuis %s (%s %s…)", client, scope.get("method", scope["type"]),
                        scope.get("path", "")[:8])
            if scope["type"] == "http":
                await send({"type": "http.response.start", "status": 404,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"Not found"})
            return None
        LOG.info("%s %s /<jeton>/%s", client, scope["method"], scope["path"].split("/", 2)[-1])
        return await self.app(scope, receive, send)


def main() -> None:
    options = json.loads(OPTIONS_FILE.read_text()) if OPTIONS_FILE.exists() else {}
    level = str(options.get("log_level", "info")).upper()
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    token = load_access_token(options)
    migrate_fit_files(DATA_DIR / "fit", FIT_DIR)
    garmin = GarminClient(options.get("garmin_api_key", ""), FIT_DIR)
    store = FicheStore(COACH_DIR)
    suivi = SuiviStore(COACH_DIR, store)
    cache = TTLCache()
    montre = MontreSync(COACH_DIR, DATA_DIR / "garmin_connect", store, suivi)
    mcp = build_server(garmin, store, suivi, cache, montre)
    app = mcp.streamable_http_app(
        streamable_http_path=f"/{token}/mcp",
        stateless_http=True,
        json_response=True,
        host="0.0.0.0",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    ssl_kwargs = {}
    if options.get("ssl", True):
        cert, key = SSL_DIR / options.get("certfile", "fullchain.pem"), SSL_DIR / options.get("keyfile", "privkey.pem")
        if not cert.exists() or not key.exists():
            raise SystemExit(f"Certificat introuvable ({cert}, {key}) : vérifiez l'add-on Let's Encrypt / Duck DNS.")
        ssl_kwargs = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}

    LOG.info("Serveur MCP prêt. URL du connecteur : https://<votre-domaine>/%s…/mcp "
             "(jeton complet dans l'option access_token)", token[:4])
    LOG.info("Panneau Coach prêt (Ingress, port %s). Fiches : %s", PANEL_PORT, COACH_DIR)

    servers = [
        uvicorn.Server(uvicorn.Config(Guard(app, token), host="0.0.0.0", port=PORT, access_log=False,
                                      log_level=level.lower(), proxy_headers=False, **ssl_kwargs)),
        uvicorn.Server(uvicorn.Config(build_panel(garmin, store, suivi, cache, montre), host="0.0.0.0", port=PANEL_PORT,
                                      access_log=False, log_level=level.lower(), proxy_headers=False)),
    ]

    threading.Thread(target=sync_loop, args=(garmin, suivi, cache), daemon=True, name="suivi").start()
    threading.Thread(target=montre.loop, daemon=True, name="montre").start()

    async def serve_all() -> None:
        await asyncio.gather(*(s.serve() for s in servers))

    asyncio.run(serve_all())

if __name__ == "__main__":
    main()
