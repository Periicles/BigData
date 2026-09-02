"""Configuration du pipeline, lue depuis l'environnement.

Aucun secret n'est écrit en dur : le sel de pseudonymisation et les mots de
passe proviennent de `.env`, qui n'est pas versionné.
"""

from __future__ import annotations

import os
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent

# Dépôt du CHU : accès en lecture seule, jamais écrit par le pipeline.
SOURCE = RACINE / "eds-chu-sujet" / "source-filestorage"

# Zone de travail : copie pseudonymisée des dépôts.
LAKE = RACINE / "lake"

SOURCES_CONNUES = ("patients", "sejours", "diagnostics", "monitoring", "referentiels")


def _charger_env(chemin: Path = RACINE / ".env") -> None:
    """Charge .env sans dépendance externe. Les variables déjà définies priment."""
    if not chemin.exists():
        return
    for ligne in chemin.read_text(encoding="utf-8").splitlines():
        ligne = ligne.strip()
        if not ligne or ligne.startswith("#") or "=" not in ligne:
            continue
        cle, _, valeur = ligne.partition("=")
        os.environ.setdefault(cle.strip(), valeur.strip())


def exiger(nom: str) -> str:
    """Retourne une variable d'environnement obligatoire, ou échoue explicitement."""
    _charger_env()
    valeur = os.environ.get(nom, "").strip()
    if not valeur:
        raise RuntimeError(
            f"Variable d'environnement manquante : {nom}. "
            "Copiez .env.example en .env et renseignez-la."
        )
    return valeur
