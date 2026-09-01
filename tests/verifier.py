"""Contrôles automatisés de l'entrepôt — trois sections indépendantes.

  pseudonymisation  les identités réelles de la source sont rejouées contre
                    l'intégralité du lake ; aucune ne doit s'y trouver
  qualite           équation de conservation bronze = silver + rejets, règles
                    métier du sujet, intégrité référentielle
  rgpd              les cinq contraintes du § 5, vérifiées sur l'entrepôt réel,
                    plus l'absence de donnée personnelle dans les journaux

Ce sont des preuves, pas des déclarations : chacune interroge l'état réel et
échoue si la propriété annoncée ne tient pas.

    python -m tests.verifier            # les trois sections
    python -m tests.verifier rgpd       # une seule
"""

from __future__ import annotations

import csv
import re
import sys
from collections import defaultdict

from eds import choisir_sections
from eds.config import LAKE, RACINE, SOURCE
from eds.lake import lister_jours, pseudonymiser
from eds.warehouse import client

ROUGE, VERT, GRIS, RAZ = "\033[31m", "\033[32m", "\033[90m", "\033[0m"


class Rapport:
    """Accumule les échecs et met en forme les lignes de contrôle."""

    def __init__(self) -> None:
        self.echecs: list[str] = []

    def titre(self, texte: str) -> None:
        print(f"\n── {texte} ──\n")

    def controle(self, libelle: str, conforme: bool, detail: str = "") -> None:
        if conforme:
            print(f"{VERT}OK{RAZ}     {libelle:54} {GRIS}{detail}{RAZ}")
        else:
            self.echecs.append(libelle)
            print(f"{ROUGE}ECHEC{RAZ}  {libelle:54} {detail}")

    def egal(self, libelle: str, obtenu, attendu) -> None:
        self.controle(libelle, obtenu == attendu,
                      f"{obtenu}" if obtenu == attendu else f"{obtenu} (attendu {attendu})")

    def anomalies(self, libelle: str, anomalies: list[str]) -> None:
        self.controle(libelle, not anomalies)
        for a in anomalies[:5]:
            print(f"         {a}")


# ── Pseudonymisation ─────────────────────────────────────────────────────
def _identites_sources() -> tuple[set[str], dict[str, str]]:
    """Identifiants réels de la source, et correspondance IPP -> pseudonyme."""
    identifiants: set[str] = set()
    correspondance: dict[str, str] = {}
    for jour in lister_jours("patients"):
        with (SOURCE / "patients" / jour / "patients.csv").open(newline="", encoding="utf-8") as f:
            for ligne in csv.DictReader(f):
                identifiants.update({ligne["nir"], ligne["nom"], ligne["prenom"],
                                     ligne["patient_id"], ligne["birth_date"]})
                correspondance[ligne["patient_id"]] = pseudonymiser(ligne["patient_id"])
    return identifiants, correspondance


def _absence_identites(identifiants: set[str]) -> list[str]:
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


def _collisions(correspondance: dict[str, str]) -> list[str]:
    """Deux patients distincts ne doivent pas partager un pseudonyme."""
    inverse = defaultdict(list)
    for ipp, pseudo in correspondance.items():
        inverse[pseudo].append(ipp)
    return [f"collision sur {p} : {ipps}" for p, ipps in inverse.items() if len(ipps) > 1]


def _jointure() -> list[str]:
    """Le pseudonyme doit rester stable entre patients et séjours."""
    anomalies = []
    for jour in lister_jours("sejours"):
        chemin_p = LAKE / "patients" / jour / "patients.csv"
        pseudos_patients = set()
        if chemin_p.exists():
            with chemin_p.open(newline="", encoding="utf-8") as f:
                pseudos_patients = {l["patient_pseudo"] for l in csv.DictReader(f)}
        with (LAKE / "sejours" / jour / "sejours.csv").open(newline="", encoding="utf-8") as f:
            pseudos_sejours = {l["patient_pseudo"] for l in csv.DictReader(f)}
        orphelins = pseudos_sejours - pseudos_patients
        if orphelins:
            anomalies.append(f"{jour} : {len(orphelins)} pseudonyme(s) de séjour absents des patients du jour")
    return anomalies


def pseudonymisation(r: Rapport) -> None:
    identifiants, correspondance = _identites_sources()
    print(f"{len(correspondance)} patients, {len(identifiants)} valeurs identifiantes contrôlées\n")
    r.anomalies("Aucune identité source présente dans le lake", _absence_identites(identifiants))
    r.anomalies("Aucune collision de pseudonyme", _collisions(correspondance))
    r.anomalies("Jointure patients <-> séjours préservée", _jointure())


# ── Qualité ──────────────────────────────────────────────────────────────
def qualite(r: Rapport) -> None:
    ch = client()
    n = lambda requete: int(ch.command(requete))

    r.titre("Équation de conservation : bronze = silver + rejets")
    for source in ("sejours", "diagnostics", "monitoring"):
        bronze = n(f"SELECT count() FROM bronze.{source}")
        silver = n(f"SELECT count() FROM silver.{source}")
        rejets = n(f"SELECT count() FROM silver.rejets WHERE source = '{source}'")
        r.egal(f"{source} : {silver} + {rejets}", silver + rejets, bronze)

    r.titre("Déduplication du snapshot cumulatif")
    r.egal("patients : 16 200 lignes -> patients distincts",
           n("SELECT count() FROM silver.patients"),
           n("SELECT uniqExact(patient_pseudo) FROM bronze.patients"))
    r.egal("aucun doublon en silver.patients",
           n("SELECT count() - uniqExact(patient_pseudo) FROM silver.patients"), 0)

    r.titre("Règles métier du sujet")
    r.egal("séjours en cours conservés (discharge_ts vide)",
           n("SELECT count() FROM silver.sejours WHERE est_en_cours = 1"), 1190)
    r.egal("aucune durée négative ne subsiste",
           n("SELECT count() FROM silver.sejours WHERE duree_jours < 0"), 0)
    r.egal("durée NULL si et seulement si séjour en cours",
           n("""SELECT count() FROM silver.sejours
                WHERE (duree_jours IS NULL) != (est_en_cours = 1)"""), 0)
    r.egal("mode de sortie vide normalisé en 'inconnu'",
           n("SELECT count() FROM silver.sejours WHERE discharge_mode = ''"), 0)
    r.egal("relevés hors plage physiologique éliminés",
           n("""SELECT count() FROM silver.monitoring
                WHERE heart_rate NOT BETWEEN 20 AND 250
                   OR spo2 NOT BETWEEN 50 AND 100
                   OR temp_c NOT BETWEEN 30 AND 45"""), 0)

    r.titre("Intégrité référentielle de silver")
    r.egal("aucun séjour orphelin de patient",
           n("""SELECT count() FROM silver.sejours
                WHERE patient_pseudo NOT IN (SELECT patient_pseudo FROM silver.patients)"""), 0)
    r.egal("aucun diagnostic orphelin de séjour",
           n("""SELECT count() FROM silver.diagnostics
                WHERE stay_id NOT IN (SELECT stay_id FROM silver.sejours)"""), 0)
    r.egal("aucun relevé orphelin de séjour",
           n("""SELECT count() FROM silver.monitoring
                WHERE stay_id NOT IN (SELECT stay_id FROM silver.sejours)"""), 0)
    r.egal("aucun service non résolu",
           n("SELECT count() FROM silver.sejours WHERE service_label = 'inconnu'"), 0)
    r.egal("aucun code CIM-10 non résolu",
           n("SELECT count() FROM silver.diagnostics WHERE libelle = 'inconnu'"), 0)


# ── RGPD ─────────────────────────────────────────────────────────────────
IDENTIFIANTS_DIRECTS = ("nir", "nom", "prenom", "birth_date", "patient_id")
BASES = ("bronze", "silver", "gold_pilotage", "gold_recherche", "ops")
PERIMETRES = {
    "eds_pilotage": {"gold_pilotage"},
    "eds_recherche": {"gold_recherche"},
    "eds_exploitation": {"bronze", "silver", "ops"},
}


def rgpd(r: Rapport) -> None:
    ch = client()

    r.titre("1. Pseudonymisation")
    trouvees = ch.query(f"""
        SELECT database, table, name FROM system.columns
        WHERE database IN {BASES} AND name IN {IDENTIFIANTS_DIRECTS}
    """).result_rows
    r.controle("aucune colonne identifiante dans l'entrepôt",
               not trouvees, f"{len(trouvees)} trouvée(s)")

    r.titre("2. Minimisation")
    for colonne in ("birth_year", "patient_pseudo", "region"):
        n = int(ch.command(f"""SELECT count() FROM system.columns
                               WHERE database = 'gold_recherche' AND name = '{colonne}'"""))
        r.controle(f"'{colonne}' absent de la base recherche", n == 0)

    r.titre("3. Cloisonnement")
    for compte, attendu in PERIMETRES.items():
        obtenu = {x[0] for x in ch.query(
            f"SELECT DISTINCT database FROM system.grants WHERE user_name = '{compte}'").result_rows}
        r.controle(f"{compte} borné à son périmètre", obtenu == attendu, f"{sorted(obtenu)}")

    # Les droits sont posés colonne par colonne : le pilotage ne doit pas
    # atteindre le pseudonyme, même dans sa propre base.
    interdites = int(ch.command("""
        SELECT count() FROM system.grants
        WHERE user_name = 'eds_pilotage'
          AND column IN ('patient_pseudo', 'stay_id', 'age_au_sejour')
    """))
    r.controle("le pilotage n'a aucun droit sur le pseudonyme",
               interdites == 0, f"{interdites} colonne(s) sensible(s) accordée(s)")

    r.titre("4. Petits effectifs")
    for table in ("coh_prevalence", "coh_description"):
        mini = int(ch.command(f"SELECT min(nb_patients) FROM gold_recherche.{table}"))
        r.controle(f"{table} : aucune cohorte < 5 patients", mini >= 5, f"plus petite = {mini}")

    r.titre("5. Traçabilité")
    requises = {"_jour_depot", "_fichier_source", "_ingested_at", "_run_id"}
    for table in ("bronze.sejours", "bronze.patients", "bronze.monitoring"):
        base, nom = table.split(".")
        colonnes = {x[0] for x in ch.query(f"""
            SELECT name FROM system.columns
            WHERE database = '{base}' AND table = '{nom}'""").result_rows}
        manquantes = sorted(requises - colonnes)
        r.controle(f"{table} porte son origine et son horodatage",
                   not manquantes, f"manque {manquantes}" if manquantes else "")
    r.controle("le journal d'exécution est alimenté",
               int(ch.command("SELECT count() FROM ops.executions")) > 0)

    r.titre("6. Les journaux sont-ils exempts de donnée personnelle ?")
    motif = r"\b[0-9a-f]{16}\b|IPP\d{7}|\b\d{15}\b"
    fichier = RACINE / "logs" / "pipeline.log"
    if fichier.exists():
        suspects = re.findall(motif, fichier.read_text(encoding="utf-8", errors="ignore"))
        r.controle("logs/pipeline.log sans identifiant patient",
                   not suspects, f"{len(suspects)} occurrence(s)")
    messages = ch.query("SELECT message FROM ops.executions WHERE message != ''").result_rows
    suspects = [m for (m,) in messages if re.search(motif, m)]
    r.controle("ops.executions sans identifiant patient",
               not suspects, f"{len(suspects)} message(s) suspect(s)")


SECTIONS = {"pseudonymisation": pseudonymisation, "qualite": qualite, "rgpd": rgpd}


def main(argv: list[str] | None = None) -> int:
    args = choisir_sections(SECTIONS, argv)
    if args is None:
        return 2

    rapport = Rapport()
    for nom in args:
        print(f"\n{'=' * 74}\n{nom.upper()}\n{'=' * 74}")
        SECTIONS[nom](rapport)

    print()
    if rapport.echecs:
        print(f"{ROUGE}{len(rapport.echecs)} contrôle(s) en échec{RAZ}\n")
        return 1
    print(f"{VERT}Tous les contrôles passent.{RAZ}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
