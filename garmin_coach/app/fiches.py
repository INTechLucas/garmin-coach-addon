"""Stockage des fiches de coaching (résumés de semaine, séances, analyses, plan).

Une fiche = un fichier JSON dans <dossier>/<type>/<id>.json, plus une copie .md lisible
à côté (pratique depuis le partage Samba). Écritures atomiques, verrou global.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

TYPES = {
    "semaine": "Résumé de la semaine",
    "seance": "Séance",
    "analyse": "Analyse",
    "plan": "Plan d'entraînement",
}
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
_WEEK = re.compile(r"^\d{4}-W\d{2}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_CONTENT = 60_000


def iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def week_bounds(week: str) -> tuple[date, date]:
    y, w = int(week[:4]), int(week[6:])
    start = date.fromisocalendar(y, w, 1)
    return start, date.fromisocalendar(y, w, 7)


def slugify(text: str) -> str:
    text = text.lower()
    for a, b in (("àâä", "a"), ("éèêë", "e"), ("îï", "i"), ("ôö", "o"), ("ùûü", "u"), ("ç", "c")):
        for ch in a:
            text = text.replace(ch, b)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:60] or "fiche"


class FicheStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()
        for t in TYPES:
            (root / t).mkdir(parents=True, exist_ok=True)

    # ---- validation -------------------------------------------------------
    @staticmethod
    def _check_type(type_: str) -> str:
        if type_ not in TYPES:
            raise ToolError(f"type doit être l'un de : {', '.join(TYPES)}")
        return type_

    @staticmethod
    def _check_id(fiche_id: str) -> str:
        if not _ID.match(fiche_id or ""):
            raise ToolError("id invalide : minuscules, chiffres et tirets uniquement (80 caractères max)")
        return fiche_id

    # ---- lecture ----------------------------------------------------------
    def _path(self, type_: str, fiche_id: str) -> Path:
        return self.root / type_ / f"{fiche_id}.json"

    def get(self, type_: str, fiche_id: str) -> dict[str, Any] | None:
        p = self._path(self._check_type(type_), self._check_id(fiche_id))
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def list(self, type_: str | None = None, semaine: str | None = None) -> list[dict[str, Any]]:
        types = [self._check_type(type_)] if type_ else list(TYPES)
        out = []
        for t in types:
            for p in (self.root / t).glob("*.json"):
                try:
                    f = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if semaine and f.get("semaine") != semaine:
                    continue
                out.append(f)
        out.sort(key=lambda f: (f.get("date") or "", f.get("mis_a_jour") or ""), reverse=True)
        return out

    # ---- écriture ---------------------------------------------------------
    def save(self, *, type_: str, titre: str, contenu: str, resume: str | None = None,
             date_: str | None = None, semaine: str | None = None, fiche_id: str | None = None) -> dict[str, Any]:
        self._check_type(type_)
        titre = (titre or "").strip()
        if not titre:
            raise ToolError("titre obligatoire")
        if len(contenu or "") > MAX_CONTENT:
            raise ToolError(f"contenu trop long ({len(contenu)} caractères, max {MAX_CONTENT})")
        if date_ and not _DATE.match(date_):
            raise ToolError("date attendue au format AAAA-MM-JJ")
        if semaine and not _WEEK.match(semaine):
            raise ToolError("semaine attendue au format AAAA-Wss (ex. 2026-W47)")
        if not semaine and date_:
            semaine = iso_week(date.fromisoformat(date_))
        if type_ == "semaine":
            if not semaine:
                semaine = iso_week(date.today())
            fiche_id = fiche_id or semaine.lower()
            date_ = date_ or week_bounds(semaine)[0].isoformat()
        if not fiche_id:
            fiche_id = f"{date_ or date.today().isoformat()}-{slugify(titre)}"
        self._check_id(fiche_id)

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            old = self.get(type_, fiche_id)
            fiche = {
                "id": fiche_id,
                "type": type_,
                "titre": titre,
                "resume": (resume or "").strip() or None,
                "date": date_,
                "semaine": semaine,
                "contenu": contenu,
                "cree_le": old["cree_le"] if old else now,
                "mis_a_jour": now,
            }
            p = self._path(type_, fiche_id)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(fiche, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(p)
            p.with_suffix(".md").write_text(f"# {titre}\n\n{contenu}\n", encoding="utf-8")
        return {"id": fiche_id, "type": type_, "statut": "mise à jour" if old else "créée", "semaine": semaine}

    def delete(self, type_: str, fiche_id: str) -> bool:
        p = self._path(self._check_type(type_), self._check_id(fiche_id))
        with self._lock:
            if not p.exists():
                return False
            p.unlink()
            p.with_suffix(".md").unlink(missing_ok=True)
        return True
