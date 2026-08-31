"""Contrôle de non-fuite : aucune donnée identifiante ne doit exister dans le lake.

Ce script est une preuve, pas une déclaration. Il rejoue les identités réelles
de la source contre l'intégralité du lake et échoue si l'une d'elles y apparaît.

Usage :  python -m tests.verifier_pseudonymisation
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict

from eds.config import LAKE, SOURCE
from eds.lake import lister_jours
from eds.pseudo import pseudonymiser

ROUGE, VERT, RAZ = "\033[31m", "\033[32m", "\033[0m"


def _identites_sources() -> tuple[set[str], dict[str, str]]:
    """Collecte les identifiants réels et la correspondance IPP -> pseudonyme."""
    identifiants: set[str] = set()
    correspondance: dict[str, str] = {}
    for jour in lister_jours("patients"):
        chemin = SOURCE / "patients" / jour / "patients.csv"
        with chemin.open(newline="", encoding="utf-8") as f:
            for ligne in csv.DictReader(f):
                identifiants.update(
                    {ligne["nir"], ligne["nom"], ligne["prenom"],
                     ligne["patient_id"], ligne["birth_date"]}
                )
                correspondance[ligne["patient_id"]] = pseudonymiser(ligne["patient_id"])
    return identifiants, correspondance


def controle_absence_identites(identifiants: set[str]) -> list[str]:
    """Cherche toute identité source dans tous les fichiers texte du lake."""
    anomalies = []
    for fichier in sorted(LAKE.rglob("*")):
        if not fichier.is_file() or fichier.suffix == ".parquet":
            continue
        contenu = fichier.read_text(encoding="utf-8", errors="ignore")
        trouves = [i for i in identifiants if i and i in contenu]
        if trouves:
            anomalies.append(f"{fichier} contient {len(trouves)} identifiant(s), ex. {trouves[:3]}")
    return anomalies


def controle_collisions(correspondance: dict[str, str]) -> list[str]:
    """Deux patients distincts ne doivent pas partager un pseudonyme."""
    inverse = defaultdict(list)
    for ipp, pseudo in correspondance.items():
        inverse[pseudo].append(ipp)
    return [f"collision sur {p} : {ipps}" for p, ipps in inverse.items() if len(ipps) > 1]


def controle_jointure() -> list[str]:
    """Le pseudonyme doit rester stable entre patients et sejours."""
    anomalies = []
    for jour in lister_jours("sejours"):
        pseudos_patients = set()
        chemin_p = LAKE / "patients" / jour / "patients.csv"
        if chemin_p.exists():
            with chemin_p.open(newline="", encoding="utf-8") as f:
                pseudos_patients = {l["patient_pseudo"] for l in csv.DictReader(f)}

        with (LAKE / "sejours" / jour / "sejours.csv").open(newline="", encoding="utf-8") as f:
            pseudos_sejours = {l["patient_pseudo"] for l in csv.DictReader(f)}

        orphelins = pseudos_sejours - pseudos_patients
        if orphelins:
            anomalies.append(
                f"{jour} : {len(orphelins)} pseudonyme(s) de séjour absents des patients du jour"
            )
    return anomalies


def main() -> int:
    identifiants, correspondance = _identites_sources()
    print(f"{len(correspondance)} patients, {len(identifiants)} valeurs identifiantes contrôlées\n")

    controles = [
        ("Aucune identité source présente dans le lake", controle_absence_identites(identifiants)),
        ("Aucune collision de pseudonyme", controle_collisions(correspondance)),
        ("Jointure patients <-> séjours préservée", controle_jointure()),
    ]

    echec = False
    for libelle, anomalies in controles:
        if anomalies:
            echec = True
            print(f"{ROUGE}ECHEC{RAZ}  {libelle}")
            for a in anomalies[:5]:
                print(f"         {a}")
        else:
            print(f"{VERT}OK{RAZ}     {libelle}")

    return 1 if echec else 0


if __name__ == "__main__":
    sys.exit(main())
