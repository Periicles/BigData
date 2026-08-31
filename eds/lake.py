"""Copie du dépôt CHU vers le lake, avec pseudonymisation au fil de l'eau.

Le dépôt `source-filestorage` est en lecture seule : le pipeline n'y écrit
jamais. Chaque fichier est lu, transformé si nécessaire, puis écrit dans
`lake/`.

Deux sources seulement portent de l'identité et sont donc transformées :
`patients` et `sejours` (qui référence le patient). Les trois autres sont
recopiées à l'octet près.

La transformation est faite **en flux**, ligne par ligne : les identités ne
sont jamais écrites sur disque, pas même dans un répertoire temporaire, et
l'empreinte mémoire ne dépend pas de la taille des fichiers.
"""

from __future__ import annotations

import csv
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from eds.config import LAKE, SOURCE, SOURCES_CONNUES
from eds.pseudo import anonymiser_ligne_patient, anonymiser_ligne_sejour

journal = logging.getLogger(__name__)

# Sources transformées, et la fonction qui traite une de leurs lignes.
TRANSFORMATIONS = {
    "patients": anonymiser_ligne_patient,
    "sejours": anonymiser_ligne_sejour,
}


@dataclass(frozen=True)
class ResultatCopie:
    source: str
    jour: str
    fichier: str
    lignes: int | None  # None pour une copie binaire (non parsée)
    pseudonymise: bool


def lister_jours(source: str) -> list[str]:
    """Jours de dépôt disponibles pour une source, dans l'ordre chronologique."""
    racine = SOURCE / source
    if not racine.is_dir():
        return []
    return sorted(d.name for d in racine.iterdir() if d.is_dir())


def jours_disponibles() -> list[str]:
    """Union des jours de dépôt, toutes sources confondues.

    Les référentiels ne sont déposés que le premier jour : on prend l'union
    plutôt que l'intersection, sinon un jour sans référentiel disparaîtrait.
    """
    jours: set[str] = set()
    for source in SOURCES_CONNUES:
        jours.update(lister_jours(source))
    return sorted(jours)


def _copier_csv_transforme(entree: Path, sortie: Path, transformer) -> int:
    """Copie un CSV en appliquant `transformer` à chaque ligne."""
    with entree.open(newline="", encoding="utf-8") as f_in:
        lecteur = csv.DictReader(f_in)
        premiere = next(lecteur, None)
        if premiere is None:
            sortie.write_text("", encoding="utf-8")
            return 0

        premiere_transformee = transformer(premiere)
        with sortie.open("w", newline="", encoding="utf-8") as f_out:
            redacteur = csv.DictWriter(f_out, fieldnames=list(premiere_transformee))
            redacteur.writeheader()
            redacteur.writerow(premiere_transformee)
            lignes = 1
            for ligne in lecteur:
                redacteur.writerow(transformer(ligne))
                lignes += 1
    return lignes


def copier_source_jour(source: str, jour: str) -> list[ResultatCopie]:
    """Copie tous les fichiers d'une source pour un jour donné.

    Idempotent : réécrit intégralement les fichiers cibles.
    """
    origine = SOURCE / source / jour
    if not origine.is_dir():
        return []

    destination = LAKE / source / jour
    destination.mkdir(parents=True, exist_ok=True)
    transformer = TRANSFORMATIONS.get(source)
    resultats = []

    for fichier in sorted(origine.iterdir()):
        if not fichier.is_file():
            continue
        cible = destination / fichier.name

        if transformer is not None and fichier.suffix == ".csv":
            lignes = _copier_csv_transforme(fichier, cible, transformer)
            resultats.append(ResultatCopie(source, jour, fichier.name, lignes, True))
        else:
            # Aucune donnée identifiante : copie fidèle, sans parsing.
            shutil.copy2(fichier, cible)
            resultats.append(ResultatCopie(source, jour, fichier.name, None, False))

    for r in resultats:
        journal.info(
            "copie lake",
            extra={
                "source": r.source,
                "jour": r.jour,
                "fichier": r.fichier,
                "lignes": r.lignes,
                "pseudonymise": r.pseudonymise,
            },
        )
    return resultats


def copier_jour(jour: str) -> list[ResultatCopie]:
    """Copie toutes les sources pour un jour de dépôt."""
    resultats = []
    for source in SOURCES_CONNUES:
        resultats.extend(copier_source_jour(source, jour))
    return resultats
