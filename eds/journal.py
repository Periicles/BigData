"""Journalisation structurée du pipeline.

Deux destinations, deux usages :
  - `logs/pipeline.log` : une ligne JSON par événement, pour l'exploitation
    et la reprise sur incident (lisible par un humain comme par un outil) ;
  - `ops.executions` dans ClickHouse : le bilan par étape, interrogeable en
    SQL et joignable aux colonnes `_run_id` des tables de données.

Aucune donnée de santé ni identifiant patient n'est journalisé : les
messages ne portent que des métadonnées d'exécution.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from eds.config import RACINE

REPERTOIRE_LOGS = RACINE / "logs"
FICHIER_LOG = REPERTOIRE_LOGS / "pipeline.log"

# Champs ajoutés par le pipeline via `extra=`, à recopier dans le JSON.
_CHAMPS_METIER = (
    "run_id",
    "etape",
    "jour",
    "lignes",
    "duree_s",
    "statut",
    "source",
    # Les référentiels ne sont pas identifiés par leur source mais par la
    # table qu'ils alimentent : sans ce champ, quatre chargements distincts
    # produisent quatre lignes de journal indiscernables.
    "table",
    "fichier",
)


class FormateurJSON(logging.Formatter):
    """Une ligne JSON par événement : lisible et exploitable par un outil."""

    def format(self, enregistrement: logging.LogRecord) -> str:
        charge = {
            "horodatage": datetime.fromtimestamp(enregistrement.created).isoformat(
                timespec="seconds"
            ),
            "niveau": enregistrement.levelname,
            "message": enregistrement.getMessage(),
        }
        for champ in _CHAMPS_METIER:
            valeur = getattr(enregistrement, champ, None)
            if valeur is not None:
                charge[champ] = valeur
        if enregistrement.exc_info:
            charge["exception"] = self.formatException(enregistrement.exc_info)
        return json.dumps(charge, ensure_ascii=False)


class FormateurConsole(logging.Formatter):
    """Sortie lisible à l'écran pendant une exécution manuelle."""

    def format(self, enregistrement: logging.LogRecord) -> str:
        parties = [
            f"{datetime.fromtimestamp(enregistrement.created):%H:%M:%S}",
            f"{enregistrement.levelname:7}",
            enregistrement.getMessage(),
        ]
        details = " ".join(
            f"{c}={getattr(enregistrement, c)}"
            for c in _CHAMPS_METIER
            if getattr(enregistrement, c, None) is not None
        )
        if details:
            parties.append(f"({details})")
        return "  ".join(parties)


def configurer(niveau: int = logging.INFO) -> None:
    """Installe les deux destinations. Idempotent."""
    REPERTOIRE_LOGS.mkdir(exist_ok=True)
    racine = logging.getLogger("eds")
    if racine.handlers:
        return

    racine.setLevel(niveau)

    fichier = logging.FileHandler(FICHIER_LOG, encoding="utf-8")
    fichier.setFormatter(FormateurJSON())
    racine.addHandler(fichier)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(FormateurConsole())
    racine.addHandler(console)
