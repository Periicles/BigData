"""Audit de conformité RGPD — les cinq contraintes du sujet.

Chaque contrainte du § 5 est vérifiée sur l'entrepôt réel, pas déclarée :

  pseudonymisation   aucune colonne identifiante n'existe dans l'entrepôt
  minimisation       l'âge fin et le pseudonyme sont absents de la recherche
  cloisonnement      chaque compte de service est borné à son périmètre
  petits effectifs   aucune cohorte de moins de 5 patients n'est diffusée
  traçabilité        origine et horodatage présents sur chaque ligne

S'y ajoute un contrôle que le sujet n'exige pas mais qu'un DPO demanderait :
les journaux d'exécution ne doivent porter aucune donnée personnelle.

Usage :  python -m tests.verifier_rgpd
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from eds.config import RACINE
from eds.warehouse import client

ROUGE, VERT, GRIS, RAZ = "\033[31m", "\033[32m", "\033[90m", "\033[0m"

IDENTIFIANTS_DIRECTS = ("nir", "nom", "prenom", "birth_date", "patient_id")
BASES = ("bronze", "silver", "gold_pilotage", "gold_recherche", "ops")

PERIMETRES = {
    "eds_pilotage": {"gold_pilotage"},
    "eds_recherche": {"gold_recherche"},
    "eds_exploitation": {"bronze", "silver", "ops"},
}


def main() -> int:
    ch = client()
    echecs: list[str] = []

    def controle(libelle: str, conforme: bool, detail: str = "") -> None:
        if conforme:
            print(f"{VERT}OK{RAZ}     {libelle:54} {GRIS}{detail}{RAZ}")
        else:
            echecs.append(libelle)
            print(f"{ROUGE}ECHEC{RAZ}  {libelle:54} {detail}")

    print("\n── 1. Pseudonymisation ──\n")
    trouvees = ch.query(f"""
        SELECT database, table, name FROM system.columns
        WHERE database IN {BASES} AND name IN {IDENTIFIANTS_DIRECTS}
    """).result_rows
    controle("aucune colonne identifiante dans l'entrepôt",
             not trouvees, f"{len(trouvees)} trouvée(s)")

    print("\n── 2. Minimisation ──\n")
    for colonne in ("birth_year", "patient_pseudo", "region"):
        n = int(ch.command(f"""SELECT count() FROM system.columns
                               WHERE database = 'gold_recherche' AND name = '{colonne}'"""))
        controle(f"'{colonne}' absent de la base recherche", n == 0)

    print("\n── 3. Cloisonnement ──\n")
    for compte, attendu in PERIMETRES.items():
        obtenu = {r[0] for r in ch.query(
            f"SELECT DISTINCT database FROM system.grants WHERE user_name = '{compte}'"
        ).result_rows}
        controle(f"{compte} borné à son périmètre",
                 obtenu == attendu, f"{sorted(obtenu)}")

    # Le compte de restitution ne doit pas atteindre le pseudonyme, même dans
    # sa propre base : les droits sont posés colonne par colonne.
    interdites = int(ch.command("""
        SELECT count() FROM system.grants
        WHERE user_name = 'eds_pilotage'
          AND column IN ('patient_pseudo', 'stay_id', 'age_au_sejour')
    """))
    controle("le pilotage n'a aucun droit sur le pseudonyme",
             interdites == 0, f"{interdites} colonne(s) sensible(s) accordée(s)")

    print("\n── 4. Petits effectifs ──\n")
    for table in ("coh_prevalence", "coh_description"):
        mini = int(ch.command(f"SELECT min(nb_patients) FROM gold_recherche.{table}"))
        controle(f"{table} : aucune cohorte < 5 patients",
                 mini >= 5, f"plus petite = {mini}")

    print("\n── 5. Traçabilité ──\n")
    for table in ("bronze.sejours", "bronze.patients", "bronze.monitoring"):
        base, nom = table.split(".")
        colonnes = {r[0] for r in ch.query(f"""
            SELECT name FROM system.columns
            WHERE database = '{base}' AND table = '{nom}'
        """).result_rows}
        requises = {"_jour_depot", "_fichier_source", "_ingested_at", "_run_id"}
        controle(f"{table} porte son origine et son horodatage",
                 requises <= colonnes, f"manque {sorted(requises - colonnes)}")

    controle("le journal d'exécution est alimenté",
             int(ch.command("SELECT count() FROM ops.executions")) > 0)

    print("\n── 6. Les journaux sont-ils exempts de donnée personnelle ? ──\n")
    fichier = RACINE / "logs" / "pipeline.log"
    if fichier.exists():
        suspects = re.findall(r"\b[0-9a-f]{16}\b|IPP\d{7}|\b\d{15}\b",
                              fichier.read_text(encoding="utf-8", errors="ignore"))
        controle("logs/pipeline.log sans identifiant patient",
                 not suspects, f"{len(suspects)} occurrence(s)")
    messages = ch.query(
        "SELECT message FROM ops.executions WHERE message != ''").result_rows
    suspects = [m for (m,) in messages if re.search(r"\b[0-9a-f]{16}\b|IPP\d{7}", m)]
    controle("ops.executions sans identifiant patient",
             not suspects, f"{len(suspects)} message(s) suspect(s)")

    print()
    if echecs:
        print(f"{ROUGE}{len(echecs)} contrôle(s) en échec{RAZ}\n")
        return 1
    print(f"{VERT}Les cinq contraintes RGPD du sujet sont satisfaites.{RAZ}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
