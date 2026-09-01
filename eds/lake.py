"""Copie du dépôt CHU vers le lake, avec pseudonymisation au fil de l'eau.

Le dépôt `source-filestorage` est en lecture seule : le pipeline n'y écrit
jamais. Seules `patients` et `sejours` portent de l'identité et sont donc
transformées ; les trois autres sources sont recopiées à l'octet près.

La transformation est faite **en flux**, ligne par ligne : les identités ne
sont jamais écrites sur disque, pas même dans un répertoire temporaire, et
l'empreinte mémoire ne dépend pas de la taille des fichiers.

Trois règles, appliquées AVANT toute écriture :

  1. `patient_id` (IPP)     -> pseudonyme HMAC-SHA256 salé, déterministe
  2. `birth_date`           -> `birth_year` (généralisation)
  3. `nir`, `nom`, `prenom` -> supprimés

Le hachage est déterministe pour que les jointures patients <-> séjours
survivent, et salé parce que l'espace des IPP est énumérable : un SHA-256 nu
serait cassable par dictionnaire en quelques secondes.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import logging
import shutil
from functools import lru_cache
from pathlib import Path

from eds.config import LAKE, SOURCE, SOURCES_CONNUES, exiger

journal = logging.getLogger(__name__)

# 16 caractères hexadécimaux = 64 bits. Pour 6 000 patients, la probabilité de
# collision est de l'ordre de 10^-12 : négligeable, et vérifiée au chargement.
LONGUEUR_PSEUDO = 16


# ── Pseudonymisation ─────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def _sel() -> bytes:
    return exiger("EDS_PSEUDO_SALT").encode("utf-8")


@lru_cache(maxsize=100_000)
def pseudonymiser(patient_id: str) -> str:
    """Pseudonyme stable et non réversible d'un identifiant patient."""
    empreinte = hmac.new(_sel(), patient_id.encode("utf-8"), hashlib.sha256)
    return empreinte.hexdigest()[:LONGUEUR_PSEUDO]


def annee_naissance(birth_date: str) -> str:
    """Généralise une date de naissance à l'année.

    Les dates sont au format ISO dans la source ; toute autre forme est une
    anomalie que l'on laisse remonter plutôt que de la deviner.
    """
    valeur = (birth_date or "").strip()
    if len(valeur) >= 4 and valeur[:4].isdigit():
        return valeur[:4]
    raise ValueError(f"Date de naissance non exploitable : {birth_date!r}")


def _ligne_patient(ligne: dict[str, str]) -> dict[str, str]:
    """Construit un dictionnaire NEUF à quatre clés.

    C'est une liste blanche, pas un filtrage : `nir`, `nom`, `prenom`,
    `patient_id` et `birth_date` ne peuvent pas survivre à cette fonction,
    et une colonne identifiante ajoutée demain à la source non plus.
    """
    return {
        "patient_pseudo": pseudonymiser(ligne["patient_id"]),
        "birth_year": annee_naissance(ligne["birth_date"]),
        "sex": ligne["sex"],
        "region_code": ligne["region_code"],
    }


def _ligne_sejour(ligne: dict[str, str]) -> dict[str, str]:
    """Remplace la référence patient. Le même sel produit le même pseudonyme
    que pour `patients` : la jointure reste possible dans l'entrepôt."""
    sortie = dict(ligne)
    sortie["patient_pseudo"] = pseudonymiser(sortie.pop("patient_id"))
    return sortie


TRANSFORMATIONS = {"patients": _ligne_patient, "sejours": _ligne_sejour}


# ── Copie ────────────────────────────────────────────────────────────────
def lister_jours(source: str) -> list[str]:
    """Jours de dépôt d'une source, dans l'ordre chronologique."""
    racine = SOURCE / source
    return (
        sorted(d.name for d in racine.iterdir() if d.is_dir())
        if racine.is_dir()
        else []
    )


def jours_disponibles() -> list[str]:
    """Union des jours de dépôt, toutes sources confondues.

    Union et non intersection : les référentiels n'étant déposés que le
    premier jour, l'intersection ferait disparaître tous les autres.
    """
    return sorted({j for s in SOURCES_CONNUES for j in lister_jours(s)})


def _copier_csv(entree: Path, sortie: Path, transformer) -> int:
    """Réécrit un CSV en transformant chaque ligne.

    L'en-tête de sortie est déduit de la première ligne transformée : les
    colonnes supprimées n'y figurent donc pas.
    """
    with entree.open(newline="", encoding="utf-8") as f_in:
        lignes = (transformer(l) for l in csv.DictReader(f_in))
        premiere = next(lignes, None)
        if premiere is None:
            sortie.write_text("", encoding="utf-8")
            return 0
        with sortie.open("w", newline="", encoding="utf-8") as f_out:
            redacteur = csv.DictWriter(f_out, fieldnames=list(premiere))
            redacteur.writeheader()
            redacteur.writerow(premiere)
            total = 1
            for ligne in lignes:
                redacteur.writerow(ligne)
                total += 1
    return total


def copier_jour(jour: str) -> tuple[int, int]:
    """Copie toutes les sources d'un jour de dépôt vers le lake.

    Retourne (fichiers copiés, lignes pseudonymisées).
    Idempotent : les fichiers cibles sont réécrits intégralement.
    """
    fichiers = lignes = 0
    for source in SOURCES_CONNUES:
        origine = SOURCE / source / jour
        if not origine.is_dir():
            continue
        destination = LAKE / source / jour
        destination.mkdir(parents=True, exist_ok=True)
        transformer = TRANSFORMATIONS.get(source)
        for fichier in sorted(f for f in origine.iterdir() if f.is_file()):
            cible = destination / fichier.name
            if transformer is not None and fichier.suffix == ".csv":
                lignes += _copier_csv(fichier, cible, transformer)
            else:
                shutil.copy2(fichier, cible)  # aucune donnée identifiante
            fichiers += 1
    journal.info(
        "copie lake",
        extra={"jour": jour, "fichiers": fichiers, "lignes": lignes},
    )
    return fichiers, lignes
