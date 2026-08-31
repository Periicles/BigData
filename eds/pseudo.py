"""Pseudonymisation des identifiants patients.

Trois opérations, appliquées AVANT toute écriture sur disque :

  1. `patient_id` (IPP)  -> pseudonyme HMAC-SHA256 salé, déterministe
  2. `birth_date`        -> `birth_year` (généralisation)
  3. `nir`, `nom`, `prenom` -> supprimés

Le hachage est déterministe pour que les jointures patients <-> séjours
survivent, et salé parce que l'espace des IPP est énumérable : un SHA-256 nu
serait cassable par dictionnaire en quelques secondes.
"""

from __future__ import annotations

import hashlib
import hmac
from functools import lru_cache

from eds.config import exiger

# 16 caractères hexadécimaux = 64 bits. Pour 6 000 patients, la probabilité de
# collision est de l'ordre de 10^-12 : négligeable, et vérifiée au chargement.
LONGUEUR_PSEUDO = 16

COLONNES_SUPPRIMEES = ("nir", "nom", "prenom")


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


def anonymiser_ligne_patient(ligne: dict[str, str]) -> dict[str, str]:
    """Applique les trois règles à une ligne de `patients.csv`."""
    return {
        "patient_pseudo": pseudonymiser(ligne["patient_id"]),
        "birth_year": annee_naissance(ligne["birth_date"]),
        "sex": ligne["sex"],
        "region_code": ligne["region_code"],
    }


def anonymiser_ligne_sejour(ligne: dict[str, str]) -> dict[str, str]:
    """Remplace la référence patient d'une ligne de `sejours.csv`.

    Le même sel produit le même pseudonyme que pour `patients` : la jointure
    entre les deux tables reste possible dans l'entrepôt.
    """
    sortie = dict(ligne)
    sortie["patient_pseudo"] = pseudonymiser(sortie.pop("patient_id"))
    return sortie
