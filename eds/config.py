"""Configuration du pipeline, lue depuis l'environnement.

Aucun secret n'est écrit en dur : le sel de pseudonymisation et les mots de
passe proviennent de `.env`, qui n'est pas versionné.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent

# Dépôt du CHU : accès en lecture seule, jamais écrit par le pipeline.
SOURCE = RACINE / "eds-chu-sujet" / "source-filestorage"

# Zone de travail : copie pseudonymisée des dépôts.
LAKE = RACINE / "lake"

# ── Ce que le lake est autorisé à contenir ───────────────────────────────
#
# CETTE DÉCLARATION EST LE CONTRAT DE SORTIE DU LAKE, PAS UNE DESCRIPTION DE
# LA SOURCE. Elle énumère, fichier par fichier, les colonnes qui ont le droit
# d'exister dans la copie. `eds.lake` projette chaque fichier dessus avant
# d'écrire : une colonne que le CHU ajouterait demain — un `patient_id` dans
# les actes, un `praticien` dans les diagnostics — n'y figure pas, donc elle
# n'atteint pas le lake, et rien n'a besoin d'être modifié pour cela.
#
# C'est une liste blanche, comme celle qui protège déjà `patients` : le
# principe vaut pour TOUTES les sources, y compris celles qui ne portent pas
# d'identité aujourd'hui. Une source non déclarée n'est pas lue ; un fichier
# non déclaré n'est pas copié.
#
# Les noms sont ceux d'APRÈS transformation : `patients` et `sejours` passent
# par la pseudonymisation, et exposent donc `patient_pseudo`, jamais
# `patient_id`.
#
# `diagnostics.json` est le seul format imbriqué : sa déclaration est un
# dictionnaire, où une valeur non vide décrit les clés admises DANS les
# objets du tableau. Projeter les seules clés de premier niveau y laisserait
# passer un champ identifiant ajouté au diagnostic lui-même.
COLONNES_LAKE = {
    "patients": {
        "patients.csv": ("patient_pseudo", "birth_year", "sex", "region_code"),
    },
    "sejours": {
        "sejours.csv": ("stay_id", "patient_pseudo", "service_code",
                        "admission_ts", "discharge_ts", "admission_mode",
                        "discharge_mode"),
    },
    "diagnostics": {
        "diagnostics.json": {"stay_id": (), "diagnostics": ("code_cim10", "type")},
    },
    "monitoring": {
        "monitoring.parquet": ("stay_id", "ts", "heart_rate", "spo2", "temp_c"),
    },
    "actes": {
        "actes.parquet": ("stay_id", "code_ccam", "acte_ts"),
    },
    "referentiels": {
        "services.csv": ("service_code", "service_label"),
        "cim10.csv": ("code_cim10", "libelle"),
        "ccam.csv": ("code_ccam", "libelle", "tarif_euros"),
        "description_service.csv": ("service_code", "categorie",
                                    "capacite_lits", "pole"),
    },
}

SOURCES_CONNUES = tuple(COLONNES_LAKE)

# ── Seuils d'alerte clinique ─────────────────────────────────────────────
#
# À ne pas confondre avec les plages de plausibilité du §3 du sujet (FC
# 20-250, SpO2 50-100, temp 30-45), qui sont des règles de VALIDITÉ de la
# donnée et vivent en silver. Ici, il s'agit de qualifier une mesure valide
# d'« en alerte » : c'est une décision clinique, pas une propriété de la
# donnée, et le sujet n'en fournit aucune valeur.
#
# Il n'existe d'ailleurs aucun seuil réglementaire. Les moniteurs de chevet
# sortent d'usine avec des valeurs par défaut que chaque service —
# réanimation, USIC, télémétrie, médecine — puis chaque soignant sont censés
# adapter au patient : bêta-bloquants, sportif, nouveau-né. Les valeurs
# ci-dessous sont donc un PARAMÈTRE D'EXPLOITATION, à valider par le corps
# médical, et surchargeable sans toucher au SQL.
#
# Les valeurs par défaut sont celles fixées par l'intervenant (feuille de
# réponses fournie) : FC < 50 ou > 100 bpm, SpO2 < 92 %, T° > 38,5 °C — un
# point de départ retenu pour reproduire l'indicateur ④ du § 4, pas une norme
# clinique figée. Un service reste libre de les ajuster via EDS_SEUIL_*.
#
# Elles sont substituées dans 31_gold_transform.sql, comme {run_id}.
SEUILS_ALERTE_DEFAUT = {
    "fc_basse": "50",  # bpm
    "fc_haute": "100",  # bpm
    "spo2_basse": "92",  # %
    "temp_haute": "38.5",  # °C
}

# Langue d'affichage de Metabase. Ce n'est pas un réglage cosmétique : elle
# commande le FORMAT DES NOMBRES. En anglais, 8 112 actes s'affichent
# « 8,112 » et 2 199 450 € « 2,199,450 » — une virgule qu'un lecteur français
# lit comme un séparateur décimal. En français, l'espace insécable sépare les
# milliers et la virgule reste décimale.
#
# Par défaut le français, puisque tout le rendu l'est. `MB_LOCALE=en` dans
# `.env` suffit à repasser l'instance en anglais : c'est une préférence de
# lecture, elle n'a pas à vivre dans le code.
LANGUE_METABASE_DEFAUT = "fr"

# Les codes que Metabase accepte réellement. Une valeur hors liste serait
# refusée par son API avec un message peu clair : autant refuser ici, à la
# frontière, comme pour les seuils.
LANGUES_METABASE = ("fr", "en")

_NOMBRE = re.compile(r"-?\d+(?:\.\d+)?")


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


def seuils_alerte() -> dict[str, str]:
    """Seuils d'alerte, surchargeables par l'environnement.

    `EDS_SEUIL_FC_BASSE=45` dans .env suffit à changer la règle sans toucher
    au SQL. Toute valeur non numérique est refusée ici, à la frontière, et
    non au moment de l'interpolation.
    """
    _charger_env()
    seuils = {}
    for nom, defaut in SEUILS_ALERTE_DEFAUT.items():
        valeur = os.environ.get(f"EDS_SEUIL_{nom.upper()}", "").strip() or defaut
        if not _NOMBRE.fullmatch(valeur):
            raise RuntimeError(
                f"Seuil d'alerte invalide : EDS_SEUIL_{nom.upper()}={valeur!r} "
                "(un nombre est attendu)."
            )
        seuils[nom] = valeur
    return seuils


def langue_metabase() -> str:
    """Langue d'affichage de Metabase, surchargeable par `MB_LOCALE`."""
    _charger_env()
    valeur = os.environ.get("MB_LOCALE", "").strip().lower() or LANGUE_METABASE_DEFAUT
    if valeur not in LANGUES_METABASE:
        raise RuntimeError(
            f"Langue Metabase invalide : MB_LOCALE={valeur!r} "
            f"(attendu : {' ou '.join(LANGUES_METABASE)})."
        )
    return valeur
