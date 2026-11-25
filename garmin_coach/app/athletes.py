"""Plusieurs athlètes dans le même add-on (binôme de swimrun).

Chaque athlète a sa clé Garmin, ses dossiers et sa connexion montre :
- share/garmin-coach/athletes/<id>/coach/  fiches, suivi.json, montre.json
- share/garmin-coach/athletes/<id>/fit/    fichiers .fit archivés
- /data/athletes/<id>/garmin_connect/      jetons Garmin Connect (privés)

Le premier athlète de la liste est l'athlète par défaut (outils MCP appelés sans `athlete`).
"""

from __future__ import annotations

import logging
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .fiches import FicheStore
from .garmin import GarminClient
from .montre import MontreSync
from .suivi import SuiviStore

LOG = logging.getLogger("garmin_coach.athletes")
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
ID_HISTORIQUE = "lucas"  # destination des données d'avant la v0.5 (un seul athlète)


def norm(text: str) -> str:
    """Minuscules sans accents, pour comparer les noms d'utilisateurs Home Assistant."""
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return t.strip().lower()


@dataclass
class Athlete:
    id: str
    nom: str
    utilisateurs: set[str]
    garmin: GarminClient
    store: FicheStore
    suivi: SuiviStore
    montre: MontreSync
    cache: Any = field(default=None)

    def info(self) -> dict[str, Any]:
        return {"id": self.id, "nom": self.nom, "cle_garmin": self.garmin.configure,
                "montre_connectee": self.montre.connecte()}


class Equipe:
    def __init__(self, athletes: list[Athlete]) -> None:
        if not athletes:
            raise SystemExit("Aucun athlète configuré.")
        self.liste = athletes
        self.par_id = {a.id: a for a in athletes}

    @property
    def defaut(self) -> Athlete:
        return self.liste[0]

    def get(self, athlete: str | None) -> Athlete:
        """Athlète par id ou par nom ; défaut si absent. Lève KeyError si inconnu."""
        if not athlete:
            return self.defaut
        key = norm(athlete)
        for a in self.liste:
            if key in (a.id, norm(a.nom)):
                return a
        raise KeyError(f"athlète inconnu : {athlete!r} (choix : {', '.join(a.id for a in self.liste)})")

    def pour_utilisateur(self, *noms: str | None) -> Athlete | None:
        """Athlète associé à un utilisateur Home Assistant (identifiant ou nom affiché)."""
        cles = {norm(n) for n in noms if n}
        for a in self.liste:
            if cles & ({a.id, norm(a.nom)} | a.utilisateurs):
                return a
        return None

    def partenaires(self, a: Athlete) -> list[Athlete]:
        return [x for x in self.liste if x.id != a.id]


def lire_config(options: dict[str, Any]) -> list[dict[str, Any]]:
    """Liste des athlètes depuis les options ; repli sur l'ancienne option garmin_api_key (v0.4)."""
    ancienne_cle = (options.get("garmin_api_key") or "").strip()
    brut = [x for x in (options.get("athletes") or []) if isinstance(x, dict)]
    if not brut:
        brut = [{"id": ID_HISTORIQUE, "nom": "Lucas"}]
    out, vus = [], set()
    for i, x in enumerate(brut):
        aid = norm(str(x.get("id") or x.get("nom") or "")).replace(" ", "-")
        if not _ID.match(aid) or aid in vus:
            raise SystemExit(f"athletes[{i}] : id invalide ou en double ({aid!r}) — minuscules, chiffres, tirets.")
        vus.add(aid)
        cle = (x.get("garmin_api_key") or "").strip() or (ancienne_cle if i == 0 else "")
        users = {norm(u) for u in re.split(r"[,;]", str(x.get("utilisateurs_ha") or "")) if u.strip()}
        out.append({"id": aid, "nom": str(x.get("nom") or aid.capitalize()).strip(), "cle": cle, "utilisateurs": users})
    return out


def _deplacer(src: Path, dst: Path) -> bool:
    if not src.is_dir() or dst.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return True


def migrer_v04(share_root: Path, data_dir: Path, premier: str) -> None:
    """Range les données d'un add-on v0.4 (un seul athlète) dans athletes/<premier>/."""
    base = share_root / "athletes" / premier
    for nom in ("coach", "fit"):
        if _deplacer(share_root / nom, base / nom):
            LOG.info("Migration : %s déplacé vers %s", share_root / nom, base / nom)
    if _deplacer(data_dir / "garmin_connect", data_dir / "athletes" / premier / "garmin_connect"):
        LOG.info("Migration : connexion Garmin Connect rattachée à %s", premier)
    # .fit encore dans /data/fit (v0.1.0)
    vieux = data_dir / "fit"
    if vieux.is_dir():
        (base / "fit").mkdir(parents=True, exist_ok=True)
        for f in vieux.glob("*.fit"):
            cible = base / "fit" / f.name
            if cible.exists():
                f.unlink()
            else:
                shutil.move(str(f), cible)
        try:
            vieux.rmdir()
        except OSError:
            pass


def construire(options: dict[str, Any], share_root: Path, data_dir: Path) -> Equipe:
    from .panel import TTLCache  # import tardif (dépendance circulaire)

    conf = lire_config(options)
    migrer_v04(share_root, data_dir, conf[0]["id"])
    athletes = []
    for c in conf:
        base = share_root / "athletes" / c["id"]
        store = FicheStore(base / "coach")
        suivi = SuiviStore(base / "coach", store)
        montre = MontreSync(base / "coach", data_dir / "athletes" / c["id"] / "garmin_connect", store, suivi)
        garmin = GarminClient(c["cle"], base / "fit", nom=c["nom"])
        athletes.append(Athlete(c["id"], c["nom"], c["utilisateurs"], garmin, store, suivi, montre, TTLCache()))
        if not c["cle"]:
            LOG.warning("Athlète %s : clé API Garmin absente (lecture Garmin indisponible pour lui/elle)", c["nom"])
    LOG.info("Athlètes : %s (par défaut : %s)", ", ".join(a.nom for a in athletes), athletes[0].nom)
    return Equipe(athletes)
