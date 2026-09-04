"""Copie du dépôt CHU vers le lake, avec pseudonymisation au fil de l'eau.

Le dépôt `source-filestorage` est en lecture seule : le pipeline n'y écrit
jamais.

AUCUN FICHIER N'EST RECOPIÉ TEL QUEL. Chaque fichier est projeté sur les
colonnes que `config.COLONNES_LAKE` l'autorise à déposer, quel que soit son
format — CSV, JSON ou Parquet. Ce qui n'est pas déclaré n'atteint pas le
lake, et un fichier dont le contenu n'est pas décrit n'est pas copié du tout.

C'est la même liste blanche que celle qui protège `patients`, généralisée à
toutes les sources. Une source qui ne porte pas d'identité aujourd'hui peut
en porter demain : `actes` et `monitoring` sont liés au patient par
`stay_id`, il suffirait d'une colonne ajoutée en amont pour que l'identité
traverse. La déclaration rend cette régression impossible sans qu'un humain
l'écrive noir sur blanc.

Sur les deux sources identifiantes, la projection vient EN PLUS de la
transformation : `patients` et `sejours` sont d'abord pseudonymisées, puis
projetées.

La transformation des CSV est faite **en flux**, ligne par ligne : les
identités ne sont jamais écrites sur disque, pas même dans un répertoire
temporaire, et l'empreinte mémoire ne dépend pas de la taille des fichiers.
Le JSON, lui, est chargé entier — un tableau JSON ne se lit pas en flux sans
analyseur incrémental, et `diagnostics.json` pèse quelques centaines de
kilo-octets. Le Parquet est projeté par DuckDB, qui ne matérialise que les
colonnes retenues.

Trois règles, appliquées AVANT toute écriture :

  1. `patient_id` (IPP)     -> pseudonyme HMAC-SHA256 salé, déterministe
  2. `birth_date`           -> `birth_year` (généralisation)
  3. `nir`, `nom`, `prenom` -> supprimés

Le hachage est déterministe pour que les jointures patients <-> séjours
survivent, et salé parce que l'espace des IPP est énumérable : un SHA-256 nu
serait cassable par dictionnaire en quelques secondes.

DEUX ENTRÉES SOURCE MALFORMÉES NE BLOQUENT PAS LE DÉPÔT DU JOUR, ET SONT
ÉCRITES TELLES QUELLES POUR ÊTRE TRACÉES EN AVAL (silver, cf.
21_silver_transform.sql) :

  · une date de naissance illisible produit un `birth_year` VIDE, jamais une
    exception — le sujet range « dates valides » parmi les contrôles à
    DÉTECTER et tracer, pas à bloquer ;
  · un `patient_id` vide (après nettoyage des espaces) produit un pseudonyme
    VIDE, SANS hachage — le hacher produirait un pseudonyme valide partagé
    par toutes les lignes sans identifiant : un faux patient à N séjours, qui
    gonflerait la réadmission.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import json
import logging
from functools import lru_cache
from pathlib import Path

import duckdb

from eds.config import COLONNES_LAKE, LAKE, SOURCE, SOURCES_CONNUES, exiger

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
    """Pseudonyme stable et non réversible d'un identifiant patient.

    Un IPP vide (après nettoyage des espaces) n'identifie personne : le
    hacher produirait un pseudonyme valide, déterministe, PARTAGÉ par toutes
    les lignes sans identifiant — un faux patient à N séjours qui gonflerait
    la réadmission. On renvoie donc une chaîne vide, sans hachage ; silver
    écarte toute ligne au pseudonyme vide (motif 'patient_manquant').
    """
    if not patient_id.strip():
        return ""
    empreinte = hmac.new(_sel(), patient_id.encode("utf-8"), hashlib.sha256)
    return empreinte.hexdigest()[:LONGUEUR_PSEUDO]


def annee_naissance(birth_date: str) -> str:
    """Généralise une date de naissance à l'année.

    Les dates sont au format ISO dans la source ; toute autre forme est une
    anomalie. On ne la laisse plus remonter comme exception : le sujet range
    « dates valides » parmi les contrôles à DÉTECTER et tracer, pas à
    bloquer — une exception ici ferait échouer l'ingestion du JOUR ENTIER
    pour un seul patient. On renvoie donc une valeur vide ; bronze la lit en
    NULL (mode tolérant, cf. eds/warehouse.py) et silver conserve le patient
    avec `birth_year` NULL, signalé en quarantaine.
    """
    valeur = (birth_date or "").strip()
    if len(valeur) >= 4 and valeur[:4].isdigit():
        return valeur[:4]
    return ""


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


# ── Projection sur les colonnes déclarées ────────────────────────────────
def _projeter(ligne: dict, colonnes) -> dict:
    """Ne garde que les colonnes déclarées, dans l'ordre de la déclaration.

    Comme `_ligne_patient`, c'est une construction, pas un filtrage : une
    colonne absente de la déclaration ne peut pas se retrouver en sortie.
    Une colonne déclarée mais absente de la source est simplement omise —
    l'écart est signalé par `_signaler_ecart`, il ne bloque pas le dépôt.
    """
    return {c: ligne[c] for c in colonnes if c in ligne}


def _signaler_ecart(fichier: Path, presentes, colonnes) -> None:
    """Journalise les deux écarts possibles entre la source et sa déclaration.

    Une colonne apparue en amont est RETIRÉE — c'est la protection qui joue,
    et elle doit se voir. Une colonne déclarée qui disparaît est une
    régression de schéma, à tracer avant que l'aval ne la constate.
    """
    retirees = [c for c in presentes if c not in colonnes]
    manquantes = [c for c in colonnes if c not in presentes]
    if retirees:
        journal.warning(
            "colonnes non déclarées retirées",
            extra={"fichier": fichier.name, "colonnes": ",".join(retirees)},
        )
    if manquantes:
        journal.warning(
            "colonnes déclarées absentes de la source",
            extra={"fichier": fichier.name, "colonnes": ",".join(manquantes)},
        )


def _litteral(chemin: Path) -> str:
    """Échappe un chemin pour l'insérer dans une chaîne SQL DuckDB."""
    return str(chemin).replace("'", "''")


# ── Copie, format par format ─────────────────────────────────────────────
def _copier_csv(entree: Path, sortie: Path, transformer, colonnes) -> int:
    """Réécrit un CSV en transformant puis en projetant chaque ligne.

    L'en-tête de sortie suit l'ordre de la déclaration : les colonnes
    supprimées et celles qui n'ont jamais été déclarées n'y figurent pas.
    """
    with entree.open(newline="", encoding="utf-8") as f_in:
        transformees = (
            transformer(l) if transformer else l for l in csv.DictReader(f_in)
        )
        # L'écart se mesure APRÈS transformation : `patients` et `sejours`
        # sortent de la pseudonymisation avec `patient_pseudo` là où la source
        # portait `patient_id`. Comparer les colonnes de la source à la
        # déclaration signalerait un écart à chaque dépôt, pour rien.
        premiere_brute = next(transformees, None)
        if premiere_brute is None:
            sortie.write_text("", encoding="utf-8")
            return 0
        _signaler_ecart(entree, list(premiere_brute), colonnes)

        premiere = _projeter(premiere_brute, colonnes)
        lignes = (_projeter(l, colonnes) for l in transformees)
        with sortie.open("w", newline="", encoding="utf-8") as f_out:
            redacteur = csv.DictWriter(f_out, fieldnames=list(premiere))
            redacteur.writeheader()
            redacteur.writerow(premiere)
            total = 1
            for ligne in lignes:
                redacteur.writerow(ligne)
                total += 1
    return total


def _copier_json(entree: Path, sortie: Path, contrat: dict) -> int:
    """Réécrit un JSON en projetant chaque objet, imbrications comprises.

    `contrat` associe à chaque clé admise les clés admises DANS les objets
    qu'elle contient, ou un tuple vide si la valeur est scalaire. Sans cette
    descente, un champ identifiant ajouté au diagnostic lui-même — un
    praticien, un identifiant de dossier — traverserait la projection de
    premier niveau sans être vu.
    """
    contenu = json.loads(entree.read_text(encoding="utf-8"))
    objets = contenu if isinstance(contenu, list) else [contenu]
    if objets:
        _signaler_ecart(entree, list(objets[0]), contrat)

    projetes = []
    for objet in objets:
        sortant = {}
        for cle, imbriquees in contrat.items():
            if cle not in objet:
                continue
            valeur = objet[cle]
            if imbriquees and isinstance(valeur, list):
                valeur = [_projeter(e, imbriquees) for e in valeur]
            sortant[cle] = valeur
        projetes.append(sortant)

    sortie.write_text(
        json.dumps(projetes if isinstance(contenu, list) else projetes[0],
                   indent=1, ensure_ascii=False),
        encoding="utf-8",
    )
    return len(projetes)


def _copier_parquet(entree: Path, sortie: Path, colonnes) -> int:
    """Réécrit un Parquet en ne sélectionnant que les colonnes déclarées.

    DuckDB ne lit que les colonnes retenues : les autres ne sont jamais
    matérialisées, pas même en mémoire.
    """
    presentes = [
        r[0] for r in duckdb.sql(
            f"DESCRIBE SELECT * FROM read_parquet('{_litteral(entree)}')"
        ).fetchall()
    ]
    _signaler_ecart(entree, presentes, colonnes)
    retenues = [c for c in colonnes if c in presentes]
    if not retenues:
        journal.error(
            "aucune colonne déclarée dans le parquet, fichier non copié",
            extra={"fichier": entree.name},
        )
        return 0

    projection = ", ".join(f'"{c}"' for c in retenues)
    duckdb.sql(
        f"COPY (SELECT {projection} "
        f"FROM read_parquet('{_litteral(entree)}')) "
        f"TO '{_litteral(sortie)}' (FORMAT PARQUET)"
    )
    return 1


def copier_jour(jour: str) -> tuple[int, int]:
    """Copie toutes les sources d'un jour de dépôt vers le lake.

    Retourne (fichiers copiés, lignes pseudonymisées).
    Idempotent : les fichiers cibles sont réécrits intégralement.

    Un fichier dont le contenu n'est pas décrit dans `COLONNES_LAKE` n'est PAS
    copié : on ne sait pas ce qu'il contient, donc on ne sait pas qu'il est
    inoffensif. Il est signalé, et le reste du dépôt passe — le sujet range
    les anomalies de source parmi les choses à détecter, pas à bloquer.
    """
    fichiers = lignes = 0
    for source in SOURCES_CONNUES:
        origine = SOURCE / source / jour
        if not origine.is_dir():
            continue
        declaration = COLONNES_LAKE[source]
        transformer = TRANSFORMATIONS.get(source)
        for fichier in sorted(f for f in origine.iterdir() if f.is_file()):
            colonnes = declaration.get(fichier.name)
            if colonnes is None:
                journal.warning(
                    "fichier non déclaré, non copié",
                    extra={"source": source, "fichier": fichier.name},
                )
                continue

            cible = LAKE / source / jour / fichier.name
            cible.parent.mkdir(parents=True, exist_ok=True)
            if fichier.suffix == ".csv":
                copiees = _copier_csv(fichier, cible, transformer, colonnes)
                if transformer is not None:
                    lignes += copiees
            elif fichier.suffix == ".json":
                _copier_json(fichier, cible, colonnes)
            elif fichier.suffix == ".parquet":
                if not _copier_parquet(fichier, cible, colonnes):
                    continue
            else:
                journal.warning(
                    "format non pris en charge, fichier non copié",
                    extra={"source": source, "fichier": fichier.name},
                )
                continue
            fichiers += 1
    journal.info(
        "copie lake",
        extra={"jour": jour, "fichiers": fichiers, "lignes": lignes},
    )
    return fichiers, lignes
