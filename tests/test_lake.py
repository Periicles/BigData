"""La copie vers le lake — pseudonymisation, généralisation, liste blanche.

C'est la frontière où l'identité disparaît. Un défaut ici ne se rattrape
nulle part en aval : une colonne identifiante écrite dans le lake y reste, et
l'entrepôt entier est construit dessus.
"""

from __future__ import annotations

import csv

import pytest

from eds import lake
from tests.conftest import deposer


# ── Pseudonymisation ─────────────────────────────────────────────────────
def test_pseudonyme_est_deterministe(sel):
    """Deux appels sur le même IPP donnent le même pseudonyme.

    C'est ce qui fait survivre la jointure patients <-> séjours : les deux
    sources sont hachées séparément, et doivent se rejoindre.
    """
    assert lake.pseudonymiser("P0001") == lake.pseudonymiser("P0001")


def test_pseudonymes_distincts_pour_ipp_distincts(sel):
    assert lake.pseudonymiser("P0001") != lake.pseudonymiser("P0002")


def test_pseudonyme_ne_contient_pas_l_ipp(sel):
    """Le pseudonyme ne doit pas laisser transparaître ce qu'il remplace."""
    pseudo = lake.pseudonymiser("P0001")
    assert "P0001" not in pseudo
    assert len(pseudo) == lake.LONGUEUR_PSEUDO
    assert all(c in "0123456789abcdef" for c in pseudo)


def test_le_sel_change_le_pseudonyme(monkeypatch, sel):
    """Sans sel, un SHA-256 nu serait cassable par dictionnaire — l'espace des
    IPP est énumérable. Ce test vérifie que le sel entre réellement dans le
    calcul, et n'est pas un paramètre décoratif."""
    avec_sel_de_test = lake.pseudonymiser("P0001")

    lake._sel.cache_clear()
    lake.pseudonymiser.cache_clear()
    monkeypatch.setenv("EDS_PSEUDO_SALT", "un-tout-autre-sel")
    assert lake.pseudonymiser("P0001") != avec_sel_de_test


@pytest.mark.parametrize("vide", ["", "   ", "\t", "\n"])
def test_ipp_vide_ne_produit_aucun_pseudonyme(sel, vide):
    """Un IPP vide n'identifie personne.

    Le hacher produirait un pseudonyme valide, déterministe, PARTAGÉ par
    toutes les lignes sans identifiant : un faux patient à N séjours, qui
    gonflerait la réadmission. On renvoie donc une chaîne vide, que silver
    écarte sous le motif 'patient_manquant'.
    """
    assert lake.pseudonymiser(vide) == ""


# ── Généralisation de la date de naissance ───────────────────────────────
@pytest.mark.parametrize(
    "entree,attendu",
    [
        ("1933-12-09", "1933"),
        ("2026-01-01", "2026"),
        ("  1975-06-30  ", "1975"),
        ("1975", "1975"),
    ],
)
def test_annee_naissance_generalise(entree, attendu):
    assert lake.annee_naissance(entree) == attendu


@pytest.mark.parametrize("illisible", ["", "   ", "inconnu", "09/12/1933", "n/a", None])
def test_date_illisible_ne_leve_pas_d_exception(illisible):
    """Le sujet range « dates valides » parmi les contrôles à DÉTECTER et
    tracer, pas à bloquer. Une exception ici ferait échouer l'ingestion du
    JOUR ENTIER pour un seul patient — bronze lit la valeur vide en NULL et
    silver signale la ligne en quarantaine."""
    assert lake.annee_naissance(illisible) == ""


# ── Liste blanche : ce qui ne peut PAS survivre ──────────────────────────
def test_ligne_patient_supprime_toute_identite(sel):
    """`_ligne_patient` construit un dictionnaire NEUF : c'est une liste
    blanche, pas un filtrage."""
    sortie = lake._ligne_patient({
        "patient_id": "P0001",
        "nir": "1750699123456",
        "nom": "Dupont",
        "prenom": "Marie",
        "birth_date": "1975-06-30",
        "sex": "F",
        "region_code": "35",
    })
    assert set(sortie) == {"patient_pseudo", "birth_year", "sex", "region_code"}
    valeurs = " ".join(sortie.values())
    for identifiant in ("P0001", "1750699123456", "Dupont", "Marie", "1975-06-30"):
        assert identifiant not in valeurs


def test_ligne_patient_ignore_une_colonne_identifiante_ajoutee(sel):
    """LE TEST QUI JUSTIFIE LA LISTE BLANCHE. Si le CHU ajoute demain une
    colonne `email` ou `telephone` à sa source, elle ne doit pas traverser —
    et rien dans le code n'a besoin d'être modifié pour cela."""
    sortie = lake._ligne_patient({
        "patient_id": "P0001", "nir": "1750699123456",
        "nom": "Dupont", "prenom": "Marie", "birth_date": "1975-06-30",
        "sex": "F", "region_code": "35",
        "email": "marie.dupont@exemple.fr",
        "telephone": "0612345678",
    })
    assert "email" not in sortie
    assert "telephone" not in sortie
    assert "exemple.fr" not in " ".join(sortie.values())


def test_ligne_sejour_remplace_la_reference_patient(sel):
    sortie = lake._ligne_sejour({
        "stay_id": "S0001", "patient_id": "P0001",
        "service_code": "CARDIO", "admission_ts": "2026-08-01 08:00:00",
    })
    assert "patient_id" not in sortie
    assert sortie["patient_pseudo"] == lake.pseudonymiser("P0001")
    # Les colonnes non identifiantes du séjour passent telles quelles.
    assert sortie["stay_id"] == "S0001"
    assert sortie["service_code"] == "CARDIO"


def test_jointure_patients_sejours_survit_a_la_pseudonymisation(sel):
    """Les deux sources sont transformées séparément : le même IPP doit y
    produire le même pseudonyme, sans quoi l'entrepôt perd le lien."""
    patient = lake._ligne_patient({
        "patient_id": "P0042", "nir": "1", "nom": "X", "prenom": "Y",
        "birth_date": "1980-01-01", "sex": "M", "region_code": "35",
    })
    sejour = lake._ligne_sejour({"stay_id": "S1", "patient_id": "P0042"})
    assert patient["patient_pseudo"] == sejour["patient_pseudo"] != ""


# ── Découverte des dépôts ────────────────────────────────────────────────
def test_jours_disponibles_est_une_union_pas_une_intersection(source):
    """Les référentiels ne sont pas déposés tous les jours : une intersection
    ferait disparaître tous les autres jours."""
    deposer(source, "sejours/2026-08-01/sejours.csv", "stay_id\n")
    deposer(source, "sejours/2026-08-02/sejours.csv", "stay_id\n")
    deposer(source, "referentiels/2026-08-01/services.csv", "service_code\n")
    deposer(source, "actes/2026-08-29/actes.parquet", "")
    assert lake.jours_disponibles() == ["2026-08-01", "2026-08-02", "2026-08-29"]


def test_lister_jours_d_une_source_absente_ne_casse_pas(source):
    assert lake.lister_jours("source_qui_n_existe_pas") == []


def test_actes_est_une_source_connue():
    """Le dépôt d'évolution serait invisible sans cette déclaration : un jour
    dont aucune source connue ne porte de dépôt n'apparaît pas dans les jours
    disponibles."""
    from eds.config import SOURCES_CONNUES

    assert "actes" in SOURCES_CONNUES


# ── Copie ────────────────────────────────────────────────────────────────
def test_copier_jour_transforme_les_csv_et_recopie_le_reste(source, lake_factice, sel):
    deposer(source, "patients/2026-08-26/patients.csv",
            "patient_id,nir,nom,prenom,birth_date,sex,region_code\n"
            "P0001,1750699123456,Dupont,Marie,1975-06-30,F,35\n")
    deposer(source, "actes/2026-08-26/actes.parquet", "octets binaires quelconques")

    fichiers, lignes = lake.copier_jour("2026-08-26")
    assert (fichiers, lignes) == (2, 1)

    copie = (lake_factice / "patients/2026-08-26/patients.csv").read_text(encoding="utf-8")
    for identifiant in ("Dupont", "Marie", "1750699123456", "P0001"):
        assert identifiant not in copie
    assert csv.DictReader(copie.splitlines()).fieldnames == [
        "patient_pseudo", "birth_year", "sex", "region_code"
    ]

    # Une source sans transformation déclarée est recopiée à l'octet près.
    assert (lake_factice / "actes/2026-08-26/actes.parquet").read_text(
        encoding="utf-8") == "octets binaires quelconques"


def test_copier_jour_est_idempotent(source, lake_factice, sel):
    """Rejouer un jour doit donner exactement le même lake, sans doublon :
    les fichiers cibles sont réécrits intégralement, jamais complétés."""
    deposer(source, "sejours/2026-08-01/sejours.csv",
            "stay_id,patient_id,service_code\nS1,P1,CARDIO\n")
    lake.copier_jour("2026-08-01")
    premier = (lake_factice / "sejours/2026-08-01/sejours.csv").read_text(encoding="utf-8")
    lake.copier_jour("2026-08-01")
    assert (lake_factice / "sejours/2026-08-01/sejours.csv").read_text(
        encoding="utf-8") == premier


def test_csv_source_vide_ne_leve_pas_d_exception(source, lake_factice, sel):
    """Un dépôt peut contenir un fichier sans aucune ligne : la copie doit le
    traverser sans exception, en produisant un fichier vide."""
    deposer(source, "patients/2026-08-26/patients.csv", "")
    fichiers, lignes = lake.copier_jour("2026-08-26")
    assert (fichiers, lignes) == (1, 0)
    assert (lake_factice / "patients/2026-08-26/patients.csv").read_text(
        encoding="utf-8") == ""
