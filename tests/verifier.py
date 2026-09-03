"""Contrôles automatisés de l'entrepôt — cinq sections indépendantes.

  pseudonymisation  les identités réelles de la source sont rejouées contre
                    l'intégralité du lake ; aucune ne doit s'y trouver
  qualite           équation de conservation bronze = silver + quarantaine,
                    règles métier du sujet, intégrité référentielle de
                    silver ET du modèle en étoile
  indicateurs       les indicateurs du § 4, calculés depuis gold : leur
                    valeur restituée, et la propriété qui la fonde
  rgpd              les cinq contraintes du § 5, vérifiées sur l'entrepôt réel,
                    plus l'absence de donnée personnelle dans les journaux
  conformite        confrontation directe aux valeurs de référence fournies
                    par l'intervenant (silver, KPI 1 à 6) — écart exact
                    exigé sur les comptages, ±0,1 sur les moyennes

Ce sont des preuves, pas des déclarations : chacune interroge l'état réel et
échoue si la propriété annoncée ne tient pas.

    python -m tests.verifier            # les cinq sections
    python -m tests.verifier rgpd       # une seule
"""

from __future__ import annotations

import csv
import json
import re
import sys
from collections import defaultdict

from eds import choisir_sections
from eds.config import LAKE, RACINE, SOURCE, seuils_alerte
from eds.lake import _ligne_patient, lister_jours, pseudonymiser
from eds.warehouse import client

# Valeurs de référence fournies par l'intervenant — fichier LOCAL, non
# versionné (cf. .gitignore) : absent sur une machine qui n'a pas reçu le
# fichier de référence, il ne doit donc jamais faire échouer la suite, seulement priver
# certains contrôles de leur cible externe.
_REFERENCE = RACINE / "eds-chu-sujet" / "corrige-kpi-niveau1.json"


def _valeurs_reference(section: str) -> dict | None:
    """Section `section` des valeurs de référence, ou None si le fichier est absent."""
    if not _REFERENCE.exists():
        return None
    return json.loads(_REFERENCE.read_text(encoding="utf-8")).get(section)


def _reference_complete() -> dict | None:
    """Les valeurs de référence entières, ou None si le fichier est absent (cf. `_REFERENCE`)."""
    if not _REFERENCE.exists():
        return None
    return json.loads(_REFERENCE.read_text(encoding="utf-8"))

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

    def valeur(self, libelle: str, valeur: str) -> None:
        """Affiche un chiffre restitué. Ce n'est pas un contrôle : c'est
        l'indicateur lui-même, montré à côté des propriétés qui le fondent."""
        print(f"{GRIS}  ·{RAZ}    {libelle:54} {valeur}")


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
    """Le pseudonyme doit rester stable entre patients et séjours.

    La population de référence est l'UNION des dépôts de `patients`, pas le
    dépôt du jour : le snapshot est cumulatif et suit son propre calendrier —
    un séjour du 1er août est décrit par un fichier patients déposé plus tard.
    Comparer jour à jour ferait passer ce décalage pour une rupture de
    jointure.
    """
    pseudos_patients: set[str] = set()
    for jour in lister_jours("patients"):
        chemin = LAKE / "patients" / jour / "patients.csv"
        if not chemin.exists():
            continue
        with chemin.open(newline="", encoding="utf-8") as f:
            pseudos_patients |= {l["patient_pseudo"] for l in csv.DictReader(f)}

    anomalies = []
    for jour in lister_jours("sejours"):
        chemin = LAKE / "sejours" / jour / "sejours.csv"
        if not chemin.exists():
            continue
        with chemin.open(newline="", encoding="utf-8") as f:
            pseudos_sejours = {l["patient_pseudo"] for l in csv.DictReader(f)}
        orphelins = pseudos_sejours - pseudos_patients
        if orphelins:
            anomalies.append(f"{jour} : {len(orphelins)} pseudonyme(s) de séjour absents du snapshot patients")
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

    r.titre("Équation de conservation : bronze = silver + quarantaine")
    for source in ("sejours", "diagnostics", "monitoring", "actes"):
        bronze = n(f"SELECT count() FROM bronze.{source}")
        silver = n(f"SELECT count() FROM silver.{source}")
        rejets = n(f"""SELECT count() FROM quarantaine.rejets
                       WHERE source = '{source}' AND action = 'ecarte'""")
        r.egal(f"{source} : {silver} + {rejets}", silver + rejets, bronze)

    r.titre("Valeurs de référence — silver (fournies par l'intervenant)")
    # Décision de l'intervenant : la cohérence temporelle d'un séjour n'écarte
    # plus ses diagnostics ni ses relevés (cf. 21_silver_transform.sql) — d'où
    # ces deux cibles, distinctes de celles qui prévalaient avant la décision.
    ref_silver = _valeurs_reference("silver")
    if ref_silver is None:
        r.controle("corrige-kpi-niveau1.json absent — contrôle ignoré", True)
    else:
        r.egal("silver.monitoring", n("SELECT count() FROM silver.monitoring"),
               ref_silver["monitoring"])
        # Le fichier fourni ne porte pas de clé 'diagnostics' : la valeur
        # 12 720 est donc donnée EN DUR ici, telle que communiquée par
        # l'intervenant dans sa décision (« silver.diagnostics = 12 720,
        # aucun écart »), plutôt que lue du fichier. Se rabattre sur
        # bronze.diagnostics ferait de ce contrôle un doublon strict de
        # l'équation de conservation ci-dessus (silver + rejets = bronze,
        # avec rejets = 0 pour cette source) : une dérive de
        # bronze.diagnostics lui-même (ingestion, dédoublonnage) ne serait
        # alors détectée par AUCUN des deux contrôles.
        r.egal("silver.diagnostics (valeur communiquée par l'intervenant)",
               n("SELECT count() FROM silver.diagnostics"),
               ref_silver.get("diagnostics", 12_720))

    r.titre("Déduplication du snapshot cumulatif")
    # L'équation n'est pas ligne à ligne (snapshot cumulatif) : ce qui se
    # compare, c'est le nombre de PATIENTS DISTINCTS identifiés (pseudonyme
    # non vide) à silver.patients — un patient_id vide n'identifie personne,
    # il est exclu des deux côtés, motif 'patient_manquant' (cf.
    # 21_silver_transform.sql).
    requete_patients_identifies = "SELECT uniqExact(patient_pseudo) FROM bronze.patients WHERE patient_pseudo != ''"
    r.egal(f"patients identifiés : {n(requete_patients_identifies)} -> silver.patients",
           n("SELECT count() FROM silver.patients"),
           n(requete_patients_identifies))
    r.egal("aucun doublon en silver.patients",
           n("SELECT count() - uniqExact(patient_pseudo) FROM silver.patients"), 0)

    # Un patient_id vide en source pseudonymise en chaîne VIDE, sans hachage
    # (cf. eds/lake.py) : un pseudonyme haché aurait été un faux patient
    # partagé par toutes les lignes sans identifiant, gonflant la
    # réadmission. Ni patients ni séjours ne doivent donc jamais porter ce
    # pseudonyme vide en silver — motif 'patient_manquant', toujours écarté.
    r.egal("aucun pseudonyme vide en silver.patients",
           n("SELECT countIf(patient_pseudo = '') FROM silver.patients"), 0)
    r.egal("aucun pseudonyme vide en silver.sejours",
           n("SELECT countIf(patient_pseudo = '') FROM silver.sejours"), 0)

    # Une correction n'est pas une exclusion : elle ne doit jamais entrer dans
    # l'équation, sinon corriger une valeur ferait disparaître une ligne du
    # compte. Le contrôle vérifie que les deux issues restent bien séparées.
    r.egal("aucune correction comptée comme une exclusion",
           n("""SELECT count() FROM quarantaine.rejets
                WHERE action NOT IN ('ecarte', 'corrige')"""), 0)

    r.titre("Règles métier du sujet")
    # Attendu dérivé de bronze, non figé : un séjour en cours ne peut être ni
    # temporellement incohérent (il n'a pas de sortie) ni porteur d'une date de
    # sortie illisible. Tous ceux dont l'admission est lisible doivent donc se
    # retrouver en silver, quel que soit le dépôt.
    r.egal("séjours en cours conservés (discharge_ts vide)",
           n("SELECT count() FROM silver.sejours WHERE est_en_cours = 1"),
           n("""SELECT count() FROM bronze.sejours
                WHERE admission_ts IS NOT NULL
                  AND _discharge_illisible = 0
                  AND discharge_ts IS NULL"""))
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
    # Règle dérivée (au-delà de la liste littérale du §3, cf. l'en-tête de
    # 21_silver_transform.sql) : un relevé hors de la fenêtre de son séjour
    # — avant l'admission, ou après la sortie d'un séjour clos — n'est pas
    # une alerte, c'est une donnée invalide. Sans ce contrôle, un tel relevé
    # traverserait silver sans bruit.
    r.egal("aucun relevé hors de la fenêtre de son séjour en silver",
           n("""SELECT count() FROM silver.monitoring AS m
                INNER JOIN silver.sejours AS s ON m.stay_id = s.stay_id
                WHERE m.ts < s.admission_ts
                   OR (s.discharge_ts IS NOT NULL AND m.ts > s.discharge_ts)"""), 0)

    # « Valeurs manquantes / formats » — la ligne du §3 que couvrent les deux
    # contrôles suivants. La source est propre aujourd'hui : ce qui est vérifié
    # ici, c'est que la règle existe et tient, pas qu'elle a eu à s'appliquer.
    r.egal("sexe normalisé : aucune valeur hors M/F/inconnu",
           n("SELECT count() FROM silver.patients WHERE sex NOT IN ('M','F','inconnu')"), 0)
    r.egal("sexe : aucune casse ni espace résiduel",
           n("SELECT count() FROM silver.patients WHERE sex != upper(trim(sex))"), 0)
    r.egal("aucune date illisible en silver",
           n("""SELECT count() FROM bronze.sejours
                WHERE (admission_ts IS NULL OR _discharge_illisible = 1)
                  AND stay_id IN (SELECT stay_id FROM silver.sejours)"""), 0)
    r.egal("une date illisible est écartée, pas confondue avec un séjour en cours",
           n("""SELECT count() FROM bronze.sejours
                WHERE _discharge_illisible = 1
                  AND stay_id NOT IN (SELECT cle FROM quarantaine.rejets
                                      WHERE source = 'sejours' AND motif = 'date_illisible')"""), 0)

    # Un patient_id vide en source ne bloque plus la journée (cf.
    # eds/lake.py) : le lake écrit une valeur vide plutôt que de lever une
    # exception, avant même que bronze ou silver n'entrent en jeu. Vérifié
    # directement sur les fonctions de transformation, à la source du
    # comportement — pas seulement sur ses conséquences en aval.
    ligne = _ligne_patient({
        "patient_id": "IPP999999", "birth_date": "n/a — illisible",
        "sex": "F", "region_code": "75",
    })
    r.egal("lake : date de naissance illisible -> birth_year vide",
           ligne["birth_year"], "")
    r.egal("lake : patient_id vide -> pseudonyme vide, sans hachage",
           pseudonymiser(""), "")
    r.egal("lake : patient_id fait uniquement d'espaces -> pseudonyme vide",
           pseudonymiser("   "), "")
    r.egal("lake : patient_id renseigné -> pseudonyme haché, non vide",
           pseudonymiser("IPP999999") != "", True)

    r.titre("Motifs de quarantaine connus, par source")
    # Liste blanche, tenue à jour à chaque nouveau motif : un motif observé qui
    # n'y figure pas — faute de frappe dans le SQL, oubli de mise à jour de
    # cette liste — se voit ici, plutôt que de s'accumuler silencieusement en
    # quarantaine sous un nom que plus rien ne documente. `releve_hors_sejour`
    # y figure alors même qu'il n'attrape aucune ligne sur ce dépôt (cf.
    # 21_silver_transform.sql, section MONITORING) : la liste documente la
    # règle qui EXISTE, pas seulement celle qui s'est déjà exercée.
    MOTIFS_CONNUS = {
        "patients": {"patient_manquant", "sexe_non_normalise", "date_naissance_illisible"},
        "sejours": {"patient_manquant", "date_illisible", "incoherence_temporelle"},
        # 'sejour_ecarte' ne porte plus que le patient manquant (patient_pseudo
        # vide) : la cohérence temporelle du séjour porteur n'écarte plus ses
        # diagnostics ni ses relevés (décision de l'intervenant, cf.
        # 21_silver_transform.sql). 'sejour_inconnu' couvre l'autre cas —
        # aucune trace du séjour porteur en bronze.
        "diagnostics": {"sejour_ecarte", "sejour_inconnu"},
        "monitoring": {"capteur_hors_plage", "sejour_ecarte", "sejour_inconnu", "releve_hors_sejour"},
        # Mêmes motifs que le monitoring, moins le contrôle physiologique
        # qui n'a pas d'objet sur un acte. Les trois n'attrapent aucune
        # ligne sur ce dépôt : ils sont démontrés par injection.
        "actes": {"sejour_ecarte", "sejour_inconnu", "acte_hors_sejour"},
    }
    r.controle("'releve_hors_sejour' figure dans la liste connue de 'monitoring'",
               "releve_hors_sejour" in MOTIFS_CONNUS["monitoring"])
    for source, motifs_attendus in MOTIFS_CONNUS.items():
        motifs_observes = {
            m for (m,) in ch.query(
                f"SELECT DISTINCT motif FROM quarantaine.rejets WHERE source = '{source}'"
            ).result_rows
        }
        inconnus = motifs_observes - motifs_attendus
        r.controle(f"{source} : aucun motif observé hors de la liste connue",
                   not inconnus, f"motif(s) inattendu(s) : {inconnus}" if inconnus else "")

    r.titre("Intégrité référentielle de silver")
    r.egal("aucun séjour orphelin de patient",
           n("""SELECT count() FROM silver.sejours
                WHERE patient_pseudo NOT IN (SELECT patient_pseudo FROM silver.patients)"""), 0)
    # Un diagnostic ou un relevé n'est plus rattaché à silver.sejours : sa
    # seule exigence est d'exister dans bronze.sejours (décision de
    # l'intervenant — la cohérence temporelle de silver.sejours n'entre pas
    # dans ce test, cf. 21_silver_transform.sql). Le rattachement au séjour
    # RETENU de silver.sejours, lui, est vérifié séparément ci-dessous via le
    # drapeau `sejour_coherent`.
    r.egal("aucun diagnostic orphelin de séjour (bronze.sejours)",
           n("""SELECT count() FROM silver.diagnostics
                WHERE stay_id NOT IN (SELECT stay_id FROM bronze.sejours)"""), 0)
    r.egal("aucun relevé orphelin de séjour (bronze.sejours)",
           n("""SELECT count() FROM silver.monitoring
                WHERE stay_id NOT IN (SELECT stay_id FROM bronze.sejours)"""), 0)
    r.egal("aucun acte orphelin de séjour (bronze.sejours)",
           n("""SELECT count() FROM silver.actes
                WHERE stay_id NOT IN (SELECT stay_id FROM bronze.sejours)"""), 0)
    # Le drapeau doit être l'exacte image de la présence dans silver.sejours —
    # ni en avance (un séjour non retenu marqué cohérent), ni en retard (un
    # séjour retenu marqué incohérent).
    r.egal("silver.diagnostics.sejour_coherent conforme à silver.sejours",
           n("""SELECT count() FROM silver.diagnostics
                WHERE sejour_coherent != (stay_id IN (SELECT stay_id FROM silver.sejours))"""), 0)
    r.egal("silver.monitoring.sejour_coherent conforme à silver.sejours",
           n("""SELECT count() FROM silver.monitoring
                WHERE sejour_coherent != (stay_id IN (SELECT stay_id FROM silver.sejours))"""), 0)
    r.egal("silver.actes.sejour_coherent conforme à silver.sejours",
           n("""SELECT count() FROM silver.actes
                WHERE sejour_coherent != (stay_id IN (SELECT stay_id FROM silver.sejours))"""), 0)

    # Le drapeau `sejour_coherent` atteste la PRÉSENCE du séjour porteur,
    # mais rien ne garantit, sans ce contrôle, que patient_pseudo,
    # service_code et admission_ts recopiés sont bien ceux de la version
    # RETENUE de bronze.sejours (même row_number() PARTITION BY stay_id
    # ORDER BY _jour_depot DESC qu'en 21_silver_transform.sql) — une
    # mauvaise clé de jointure, service_code et patient_pseudo inversés, ou
    # une version non retenue en cas de doublon, passerait inaperçus tant
    # qu'ils ne cassent ni l'équation de conservation ni ce drapeau. Contrôle
    # en VOLUME sur les 12 720 diagnostics et 40 920 relevés réels — pas
    # seulement sur l'unique cas synthétique de tests.demontrer qualite.
    version_retenue = """
        SELECT stay_id, patient_pseudo, service_code, admission_ts
        FROM (
            SELECT stay_id, patient_pseudo, service_code, admission_ts,
                   row_number() OVER (
                       PARTITION BY stay_id ORDER BY _jour_depot DESC
                   ) AS rang
            FROM bronze.sejours
        )
        WHERE rang = 1
    """
    for table in ("diagnostics", "monitoring", "actes"):
        r.egal(f"silver.{table} : patient/service/admission conformes à la version retenue de bronze.sejours",
               n(f"""SELECT count() FROM silver.{table} AS x
                     INNER JOIN ({version_retenue}) AS s ON x.stay_id = s.stay_id
                     WHERE x.patient_pseudo != s.patient_pseudo
                        OR x.service_code   != s.service_code
                        OR (x.admission_ts IS NULL) != (s.admission_ts IS NULL)
                        OR (x.admission_ts IS NOT NULL AND x.admission_ts != s.admission_ts)"""), 0)

    r.egal("aucun service non résolu",
           n("SELECT count() FROM silver.sejours WHERE service_label = 'inconnu'"), 0)
    r.egal("aucun code CIM-10 non résolu",
           n("SELECT count() FROM silver.diagnostics WHERE libelle = 'inconnu'"), 0)
    r.egal("aucun code CCAM non résolu",
           n("SELECT count() FROM silver.actes WHERE libelle = 'inconnu'"), 0)

    # Le fait se construit SUR les dimensions : toute clé étrangère d'un fait
    # doit désigner un membre existant. Sans ce contrôle, une clé orpheline
    # ne se verrait qu'à la restitution — la ligne disparaîtrait d'un graphe
    # joint à la dimension, silencieusement.
    r.titre("Intégrité du modèle en étoile : fait -> dimension")
    for fait, cle, dimension in (
        ("fact_sejour",     "patient_pseudo", "dim_patient"),
        ("fact_sejour",     "service_code",   "dim_service"),
        ("fact_diagnostic", "patient_pseudo", "dim_patient"),
        ("fact_diagnostic", "code_cim10",     "dim_cim10"),
        ("fact_diagnostic", "service_code",   "dim_service"),
        ("fact_releve",     "patient_pseudo", "dim_patient"),
        ("fact_releve",     "service_code",   "dim_service"),
    ):
        r.egal(f"{fait}.{cle} -> {dimension}",
               n(f"""SELECT count() FROM gold_pilotage.{fait}
                     WHERE {cle} NOT IN (SELECT {cle} FROM gold_pilotage.{dimension})"""), 0)

    # L'autre moitié de l'intégrité référentielle : une clé de dimension doit
    # être UNIQUE. Une nomenclature livrée avec une ligne dupliquée passerait
    # dans la dimension, et chaque jointure fait -> dimension doublerait les
    # lignes du service concerné — la DMS compterait ses séjours deux fois,
    # sans qu'aucune erreur ne soit levée.
    for dimension, cle in (
        ("dim_patient", "patient_pseudo"),
        ("dim_service", "service_code"),
        ("dim_cim10",   "code_cim10"),
    ):
        r.egal(f"{dimension}.{cle} est unique",
               n(f"SELECT count() - uniqExact({cle}) FROM gold_pilotage.{dimension}"), 0)

    # L'âge est dérivé du fait CONTRE la dimension. Depuis que `birth_year`
    # est Nullable (date de naissance illisible, tracée dès silver), un âge
    # manquant n'est plus forcément une jointure ratée — il peut légitimement
    # venir d'un `birth_year` NULL en dimension. Les deux causes sont donc
    # distinguées : l'orphelin fait -> dimension est déjà couvert plus haut
    # (`fact_sejour.patient_pseudo -> dim_patient`) ; ici, on vérifie que
    # l'absence d'âge coïncide EXACTEMENT avec l'absence de birth_year, ni
    # plus (jointure qui se dégraderait) ni moins (calcul qui masquerait un
    # NULL).
    # fact_sejour : une seule cause d'âge manquant — birth_year absent en
    # dimension. admission_ts n'y est jamais NULL (silver.sejours écarte tout
    # séjour à admission illisible), donc pas de second facteur à couvrir ici.
    r.egal("fact_sejour.age_au_sejour NULL si et seulement si dim_patient.birth_year NULL",
           n("""SELECT count() FROM gold_pilotage.fact_sejour AS f
                INNER JOIN gold_pilotage.dim_patient AS p USING (patient_pseudo)
                WHERE (f.age_au_sejour IS NULL) != (p.birth_year IS NULL)"""), 0)
    r.egal("fact_sejour.tranche_age 'inconnu' si et seulement si dim_patient.birth_year NULL",
           n("""SELECT count() FROM gold_pilotage.fact_sejour AS f
                INNER JOIN gold_pilotage.dim_patient AS p USING (patient_pseudo)
                WHERE (f.tranche_age = 'inconnu') != (p.birth_year IS NULL)"""), 0)

    # fact_diagnostic : DEUX causes INDÉPENDANTES d'un âge manquant — un
    # birth_year absent en dimension, OU une date_admission NULLE (séjour au
    # patient identifié mais à admission illisible, silver.diagnostics porte
    # alors admission_ts NULL — cf. 31_gold_transform.sql). Un contrôle qui
    # ne testerait que birth_year échouerait à tort sur ce second cas,
    # pourtant conforme à la spec (démontré par tests.demontrer qualite,
    # DEMO_SEJOUR_ADM_NULL) — d'où le OR ci-dessous, absent de fact_sejour.
    r.egal("fact_diagnostic.age_au_sejour NULL si et seulement si birth_year ou date_admission NULL",
           n("""SELECT count() FROM gold_pilotage.fact_diagnostic AS f
                INNER JOIN gold_pilotage.dim_patient AS p USING (patient_pseudo)
                WHERE (f.age_au_sejour IS NULL)
                      != (p.birth_year IS NULL OR f.date_admission IS NULL)"""), 0)
    r.egal("fact_diagnostic.tranche_age 'inconnu' si et seulement si birth_year ou date_admission NULL",
           n("""SELECT count() FROM gold_pilotage.fact_diagnostic AS f
                INNER JOIN gold_pilotage.dim_patient AS p USING (patient_pseudo)
                WHERE (f.tranche_age = 'inconnu')
                      != (p.birth_year IS NULL OR f.date_admission IS NULL)"""), 0)
    r.egal("silver ne calcule plus d'âge ni d'alerte",
           n("""SELECT count() FROM system.columns
                WHERE database = 'silver'
                  AND name IN ('age_au_sejour', 'alerte_fc', 'alerte_spo2',
                               'alerte_temp', 'en_alerte')"""), 0)


# ── Indicateurs ──────────────────────────────────────────────────────────
# Une table d'indicateur agrégé est une COPIE dérivée du fait. Elle peut donc
# diverger : un TRUNCATE oublié, un filtre qui change d'un côté seulement, et
# la table continue de servir un chiffre que plus rien ne fonde. Chaque table
# est donc confrontée à son recalcul depuis les faits — même clé, même valeur,
# même nombre de lignes.
COHERENCE_KPI = {
    "kpi_dms_service": (
        "service_code, mois, dms_jours AS mesure, nb_sejours_clos AS effectif",
        """SELECT service_code, toStartOfMonth(date_admission) AS mois,
                  round(avg(duree_jours), 2) AS mesure, count() AS effectif
           FROM gold_pilotage.fact_sejour WHERE est_en_cours = 0
           GROUP BY service_code, mois""",
        ("service_code", "mois"),
    ),
    "kpi_urgences_jour": (
        # `nb_passages_urgences` (service URGENCES) est la mesure confrontée
        # au fait ; `nb_admissions_en_urgence` (mode d'admission) est vérifiée
        # séparément dans indicateurs(), au même titre que ce second axe.
        "jour, nb_passages_urgences AS mesure, nb_sejours AS effectif",
        """SELECT date_admission AS jour, countIf(service_code = 'URGENCES') AS mesure,
                  count() AS effectif
           FROM gold_pilotage.fact_sejour GROUP BY jour""",
        ("jour",),
    ),
    "kpi_readmission_service": (
        # BRUT — définition de référence de l'intervenant : dénominateur =
        # tous les séjours. La variante AJUSTÉE est vérifiée séparément dans
        # indicateurs(), les deux définitions étant portées côte à côte.
        "service_code, nb_readmis_30j_brut AS mesure, nb_sejours AS effectif",
        """SELECT service_code, sum(readmission_30j_brute) AS mesure,
                  count() AS effectif
           FROM gold_pilotage.fact_sejour GROUP BY service_code""",
        ("service_code",),
    ),
    "kpi_alertes_jour": (
        "jour, service_code, nb_en_alerte AS mesure, nb_releves AS effectif",
        """SELECT date_mesure AS jour, service_code,
                  countIf(en_alerte) AS mesure, count() AS effectif
           FROM gold_pilotage.fact_releve GROUP BY jour, service_code""",
        ("jour", "service_code"),
    ),
    "kpi_mortalite_service": (
        "service_code, nb_deces AS mesure, nb_sejours_clos AS effectif",
        """SELECT service_code, countIf(discharge_mode = 'deces') AS mesure,
                  count() AS effectif
           FROM gold_pilotage.fact_sejour WHERE est_en_cours = 0
           GROUP BY service_code""",
        ("service_code",),
    ),
    "kpi_casemix_service": (
        "service_code, code_cim10, nb_sejours AS mesure, nb_sejours AS effectif",
        """SELECT service_code, code_cim10, count() AS mesure, count() AS effectif
           FROM gold_pilotage.fact_diagnostic WHERE est_principal = 1
           GROUP BY service_code, code_cim10""",
        ("service_code", "code_cim10"),
    ),
    # Les deux mesures de la table (nb_patients ET nb_sejours) sont ici
    # confrontées à DEUX recalculs indépendants — pas le même recalcul dupliqué
    # deux fois. Sans ça, un `uniqExact` remplacé par erreur par `count()`
    # donnerait nb_patients = nb_sejours partout : une valeur fausse, mais
    # toujours <= nb_sejours, qui passerait le garde-fou de l'indicateur ⑩
    # sans qu'aucun contrôle ne la détecte.
    "kpi_origine_service": (
        "service_code, region_code, nb_patients AS mesure, nb_sejours AS effectif",
        """SELECT f.service_code, p.region AS region_code,
                  uniqExact(f.patient_pseudo) AS mesure, count() AS effectif
           FROM gold_pilotage.fact_sejour AS f
           INNER JOIN gold_pilotage.dim_patient AS p ON f.patient_pseudo = p.patient_pseudo
           GROUP BY f.service_code, region_code""",
        ("service_code", "region_code"),
    ),
}


def _coherence_kpi(r: Rapport, table: str) -> None:
    """Confronte une table d'indicateur au recalcul depuis les faits."""
    ch = client()
    n = lambda requete: int(ch.command(requete))
    colonnes, recalcul, cles = COHERENCE_KPI[table]
    jointure = ", ".join(cles)

    # Deux contrôles, et il en faut deux : des cardinalités égales avec des
    # valeurs fausses passeraient le premier, des valeurs justes sur un sous-
    # ensemble passeraient le second.
    r.egal(f"{table} : autant de lignes que d'agrégats de faits",
           n(f"SELECT count() FROM gold_pilotage.{table}"),
           n(f"SELECT count() FROM ({recalcul})"))
    r.egal(f"{table} : aucune valeur qui diverge du fait",
           n(f"""SELECT count() FROM (SELECT {colonnes}
                                      FROM gold_pilotage.{table}) AS k
                 INNER JOIN ({recalcul}) AS f USING ({jointure})
                 WHERE abs(k.mesure - f.mesure) > 0.005
                    OR k.effectif != f.effectif"""), 0)


def indicateurs(r: Rapport) -> None:
    """Les indicateurs du §4, calculés depuis gold et affichés.

    Les quatre indicateurs nommés du pilotage, les deux de la recherche, et
    les quatre vues qui remplissent la cinquième ligne — « toute autre vue
    d'activité pertinente ».

    Un indicateur juste par construction ne prouve rien : chaque bloc montre
    la valeur restituée ET la propriété qui la fonde. Si la propriété tombe,
    le chiffre affiché à côté est faux — et le contrôle échoue avant qu'il ne
    soit diffusé.
    """
    ch = client()
    n = lambda requete: int(ch.command(requete))
    lignes = lambda requete: ch.query(requete).result_rows

    # Un indicateur ne se calcule pas sur un entrepôt vide : sans ce garde-fou,
    # la section échouerait sur une division par zéro, ce qui ne dit rien de la
    # propriété. On énonce la cause réelle — gold n'a pas été construit.
    vides = [t for t in ("fact_sejour", "fact_diagnostic", "fact_releve")
             if n(f"SELECT count() FROM gold_pilotage.{t}") == 0]
    r.controle("la couche gold est peuplée", not vides,
               f"table(s) vide(s) : {', '.join(vides)} — lancer `python -m eds.run`"
               if vides else "")
    if vides:
        return

    # ① DMS par service ---------------------------------------------------
    # La durée n'existe que sur un séjour clos : un séjour en cours n'a pas de
    # sortie, l'inclure au dénominateur écraserait la moyenne vers le bas.
    r.titre("① Durée moyenne de séjour, par service")
    dms = lignes("""
        SELECT s.service, round(avg(f.duree_jours), 2), count()
        FROM gold_pilotage.fact_sejour AS f
        INNER JOIN gold_pilotage.dim_service AS s USING (service_code)
        WHERE f.est_en_cours = 0
        GROUP BY s.service ORDER BY 2 DESC""")
    for service, moyenne, nb in dms:
        r.valeur(f"{service}", f"{moyenne:>6} jours   {GRIS}sur {nb} séjours clos{RAZ}")
    r.egal("un service du référentiel sans DMS",
           n("SELECT count() FROM gold_pilotage.dim_service") - len(dms), 0)
    r.egal("le dénominateur est bien l'ensemble des séjours clos",
           sum(l[2] for l in dms),
           n("SELECT countIf(est_en_cours = 0) FROM gold_pilotage.fact_sejour"))
    r.egal("aucune durée manquante sur un séjour clos",
           n("""SELECT count() FROM gold_pilotage.fact_sejour
                WHERE est_en_cours = 0 AND duree_jours IS NULL"""), 0)
    _coherence_kpi(r, "kpi_dms_service")
    # La dispersion n'a pas de valeur « attendue » à comparer : ce qui se
    # vérifie, c'est l'ordre. Une médiane au-dessus du P90, ou un P90
    # au-dessus du maximum, signalerait un quantile calculé sur le mauvais
    # sous-ensemble — l'erreur passerait inaperçue à l'œil.
    r.egal("dispersion ordonnée : médiane <= P90 <= max",
           n("""SELECT count() FROM gold_pilotage.kpi_dms_service
                WHERE NOT (mediane_jours <= p90_jours AND p90_jours <= max_jours)"""), 0)
    # dms_heures est la même moyenne que dms_jours, en heures — recalculée
    # indépendamment (et non dms_jours * 24) pour ne pas composer deux
    # arrondis dans le calcul lui-même. La tolérance encaisse ceux qui
    # restent légitimes une fois les deux colonnes déjà arrondies : jusqu'à
    # 0,005 jour (0,12 h) sur dms_jours, plus 0,05 h sur dms_heures.
    r.egal("dms_heures cohérente avec dms_jours (±0,2 h)",
           n("""SELECT count() FROM gold_pilotage.kpi_dms_service
                WHERE abs(dms_heures - dms_jours * 24) > 0.2"""), 0)

    # ② Passages aux urgences par jour ------------------------------------
    # Deux lectures, cf. § 2.10 du rapport : `nb_passages_urgences` (service
    # URGENCES) est la définition de référence retenue pour l'indicateur
    # nommé au § 4 ; `nb_admissions_en_urgence` (mode d'admission, tous
    # services) reste exposée à côté, en mesure complémentaire.
    r.titre("② Passages aux urgences, par jour")
    nb_passages = n("SELECT countIf(service_code = 'URGENCES') FROM gold_pilotage.fact_sejour")
    nb_admissions_urgence = n("SELECT countIf(est_urgence) FROM gold_pilotage.fact_sejour")
    serie = lignes("""
        SELECT count(), min(nb), round(avg(nb), 1), max(nb), sum(nb) FROM (
            SELECT date_admission, countIf(service_code = 'URGENCES') AS nb
            FROM gold_pilotage.fact_sejour GROUP BY date_admission)""")[0]
    jours, mini, moyen, maxi, cumul = serie
    r.valeur("passages, service URGENCES (référence)", f"{nb_passages} séjours")
    r.valeur("admissions en mode urgence (complémentaire)",
             f"{nb_admissions_urgence} séjours, tous services")
    r.valeur(f"passages par jour sur {jours} jours", f"min {mini} · moyenne {moyen} · max {maxi}")
    r.egal("la série journalière somme au total", cumul, nb_passages)
    # L'axe est la date d'ADMISSION du fait, jamais le jour de dépôt : les
    # deux diffèrent, et confondre les deux daterait mal chaque passage.
    r.egal("est_urgence ne qualifie que les admissions en urgence",
           n("""SELECT count() FROM gold_pilotage.fact_sejour
                WHERE est_urgence != (admission_mode = 'urgence')"""), 0)
    _coherence_kpi(r, "kpi_urgences_jour")
    r.egal("kpi_urgences_jour.nb_admissions_en_urgence cohérent avec le fait",
           n("SELECT sum(nb_admissions_en_urgence) FROM gold_pilotage.kpi_urgences_jour"),
           nb_admissions_urgence)

    # ③ Taux de réadmission à 30 jours ------------------------------------
    # Numérateur et dénominateur sont posés dans le fait, pas laissés au
    # consommateur : c'est une auto-jointure, et deux calculs concurrents
    # donneraient deux taux. Deux définitions, l'une à côté de l'autre — cf.
    # l'en-tête de kpi_readmission_service en 30_gold.sql.
    r.titre("③ Taux de réadmission à 30 jours")

    # BRUT — définition de référence de l'intervenant : dénominateur = TOUS
    # les séjours, numérateur = tout séjour clos (décès compris) suivi d'une
    # réadmission du même patient sous 30 j.
    total_, brut = lignes("""
        SELECT count(), sum(readmission_30j_brute)
        FROM gold_pilotage.fact_sejour""")[0]
    r.valeur("taux brut (référence § 4)", f"{round(100 * brut / total_, 2)} %   "
                     f"{GRIS}{brut} réadmissions sur {total_} séjours{RAZ}")
    r.controle("le numérateur brut est inclus dans le dénominateur brut", brut <= total_,
               f"{brut} / {total_}")
    _coherence_kpi(r, "kpi_readmission_service")

    # AJUSTÉ — dénominateur restreint aux séjours clos et non décédés : un
    # patient réadmis après un décès enregistré est une incohérence de
    # saisie, que cette variante exclut. La référence, elle, ne l'exclut pas
    # — c'est pourquoi le taux BRUT ci-dessus est celui publié au § 4.
    index_, readmis = lignes("""
        SELECT sum(est_sejour_index), sum(suivi_readmission_30j)
        FROM gold_pilotage.fact_sejour""")[0]
    r.valeur("taux ajusté (complément documenté)",
             f"{round(100 * readmis / index_, 2)} %   "
             f"{GRIS}{readmis} réadmissions sur {index_} séjours index{RAZ}")
    r.controle("le numérateur ajusté est inclus dans le dénominateur ajusté", readmis <= index_,
               f"{readmis} / {index_}")
    r.egal("aucune réadmission ajustée portée par un séjour hors dénominateur ajusté",
           n("""SELECT count() FROM gold_pilotage.fact_sejour
                WHERE suivi_readmission_30j = 1 AND est_sejour_index = 0"""), 0)
    # Un patient déclaré décédé puis « réadmis » fausserait le taux ajusté :
    # cette variante exclut ces séjours du dénominateur (la variante brute,
    # elle, les garde — c'est toute la différence entre les deux).
    r.egal("aucun séjour clos par décès dans le dénominateur ajusté",
           n("""SELECT count() FROM gold_pilotage.fact_sejour
                WHERE est_sejour_index = 1 AND discharge_mode = 'deces'"""), 0)
    r.egal("aucun séjour en cours dans le dénominateur ajusté",
           n("""SELECT count() FROM gold_pilotage.fact_sejour
                WHERE est_sejour_index = 1 AND est_en_cours = 1"""), 0)
    r.egal("kpi_readmission_service (ajusté) : dénominateur cohérent avec le fait",
           n("SELECT sum(nb_sejours_index) FROM gold_pilotage.kpi_readmission_service"),
           index_)
    r.egal("kpi_readmission_service (ajusté) : numérateur cohérent avec le fait",
           n("SELECT sum(nb_readmis_30j) FROM gold_pilotage.kpi_readmission_service"),
           readmis)

    # ④ Relevés en alerte -------------------------------------------------
    r.titre("④ Relevés de constantes en alerte")
    seuils = seuils_alerte()
    total, alertes, a_fc, a_spo2, a_temp = lignes("""
        SELECT count(), countIf(en_alerte), countIf(alerte_fc),
               countIf(alerte_spo2), countIf(alerte_temp)
        FROM gold_pilotage.fact_releve""")[0]
    r.valeur("relevés en alerte", f"{alertes} sur {total}   "
                                  f"{GRIS}{round(100 * alertes / total, 1)} %{RAZ}")
    r.valeur("par motif", f"FC {a_fc} · SpO2 {a_spo2} · température {a_temp}")
    r.egal("en_alerte est bien la réunion des trois motifs",
           n("""SELECT count() FROM gold_pilotage.fact_releve
                WHERE en_alerte != (alerte_fc OR alerte_spo2 OR alerte_temp)"""), 0)
    # Le contrôle qui compte : les seuils sont un PARAMÈTRE d'exploitation.
    # Recalculer les drapeaux depuis eds/config.py prouve que la valeur
    # configurée est bien celle qui a servi, et non une constante figée en SQL.
    r.egal(f"seuils appliqués = ceux de la configuration "
           f"(FC {seuils['fc_basse']}–{seuils['fc_haute']}, "
           f"SpO2 {seuils['spo2_basse']}, T° {seuils['temp_haute']})",
           n(f"""SELECT count() FROM gold_pilotage.fact_releve
                 WHERE en_alerte != (heart_rate < {seuils['fc_basse']}
                                  OR heart_rate > {seuils['fc_haute']}
                                  OR spo2 < {seuils['spo2_basse']}
                                  OR temp_c > {seuils['temp_haute']})"""), 0)
    _coherence_kpi(r, "kpi_alertes_jour")

    # ⑤ Prévalence par pathologie -----------------------------------------
    # `nb_patients` — définition de référence de l'intervenant — compte sur
    # TOUS les types de diagnostic (principal et associé), séjours
    # incohérents compris ; c'est elle qui porte le filtre k >= 5.
    # `nb_patients_principal` (ancienne définition, motif d'hospitalisation
    # seul) reste affichée à côté, pour comparaison.
    r.titre("⑤ Prévalence par pathologie (tous diagnostics, référence)")
    for pathologie, patients, patients_princ, sejours in lignes("""
            SELECT pathologie, nb_patients, nb_patients_principal, nb_sejours
            FROM gold_recherche.coh_prevalence ORDER BY nb_patients DESC"""):
        r.valeur(pathologie[:44],
                 f"{patients:>5} patients   "
                 f"{GRIS}dont {patients_princ} en motif principal · {sejours} séjours{RAZ}")
    r.egal("la prévalence reproduit exactement l'agrégat des faits",
           n("SELECT count() FROM gold_recherche.coh_prevalence"),
           n("""SELECT count() FROM (
                    SELECT code_cim10, uniqExact(patient_pseudo) AS nb
                    FROM gold_pilotage.fact_diagnostic
                    GROUP BY code_cim10 HAVING nb >= 5)"""))
    r.egal("un patient ne peut pas avoir plus de séjours que lui-même",
           n("""SELECT count() FROM gold_recherche.coh_prevalence
                WHERE nb_sejours < nb_patients"""), 0)
    r.egal("nb_patients_principal ne peut pas dépasser nb_patients",
           n("""SELECT count() FROM gold_recherche.coh_prevalence
                WHERE nb_patients_principal > nb_patients"""), 0)
    r.egal("toute pathologie restituée existe dans la nomenclature",
           n("""SELECT count() FROM gold_recherche.coh_prevalence
                WHERE code_cim10 NOT IN
                      (SELECT code_cim10 FROM gold_pilotage.dim_cim10)"""), 0)

    # ⑥ Distribution par âge et sexe --------------------------------------
    r.titre("⑥ Distribution par âge et sexe")
    r.valeur("cohortes décrites",
             f"{n('SELECT count() FROM gold_recherche.coh_description')} "
             f"(pathologie × tranche d'âge × sexe)")
    for tranche, cohortes, patients in lignes("""
            SELECT tranche_age, count(), sum(nb_patients)
            FROM gold_recherche.coh_description
            GROUP BY tranche_age ORDER BY tranche_age"""):
        r.valeur(f"tranche {tranche}", f"{cohortes:>3} cohortes   {GRIS}{patients} patients{RAZ}")
    r.egal("aucune tranche d'âge non résolue",
           n("""SELECT count() FROM gold_recherche.coh_description
                WHERE tranche_age = 'inconnu'"""), 0)
    r.egal("sexe restreint à la nomenclature",
           n("""SELECT count() FROM gold_recherche.coh_description
                WHERE sexe NOT IN ('M', 'F', 'inconnu')"""), 0)
    # Un patient occupe une seule case (tranche × sexe) : la somme des cases
    # d'une pathologie ne peut donc pas dépasser son effectif total. Elle est
    # inférieure quand une case a été supprimée par le seuil des 5.
    r.egal("la description ne peut pas compter plus de patients que la prévalence",
           n("""SELECT count() FROM (
                    SELECT d.code_cim10, sum(d.nb_patients) AS decrits,
                           any(p.nb_patients) AS total
                    FROM gold_recherche.coh_description AS d
                    INNER JOIN gold_recherche.coh_prevalence AS p USING (code_cim10)
                    GROUP BY d.code_cim10 HAVING decrits > total)"""), 0)
    r.egal("aucune pathologie décrite hors de la prévalence",
           n("""SELECT count() FROM gold_recherche.coh_description
                WHERE code_cim10 NOT IN
                      (SELECT code_cim10 FROM gold_recherche.coh_prevalence)"""), 0)

    # ⑦ Occupation ---------------------------------------------------------
    # « Toute autre vue d'activité pertinente » (§4). Celle-ci croise le flux
    # et la durée : à activité égale, un service dont la DMS double occupe
    # deux fois plus de lits.
    r.titre("⑦ Occupation — patients présents par jour et par service")
    debut, fin = lignes("SELECT min(jour), max(jour) FROM gold_pilotage.kpi_occupation_jour")[0]
    r.valeur("période couverte", f"{debut} -> {fin}")
    for service, moyenne, pic in lignes("""
            SELECT service, round(avg(nb_presents)), max(nb_presents)
            FROM gold_pilotage.kpi_occupation_jour
            GROUP BY service ORDER BY 2 DESC LIMIT 4"""):
        r.valeur(f"{service}", f"{moyenne:>5.0f} présents en moyenne   {GRIS}pic à {pic}{RAZ}")
    # Chaque séjour est admis une fois et une seule : dérouler l'intervalle
    # ne doit pas créer d'admission. Sans ce contrôle, un décalage de borne
    # dans `range()` se verrait seulement sur la courbe.
    r.egal("chaque séjour compté une fois et une seule à l'admission",
           n("SELECT sum(nb_admissions) FROM gold_pilotage.kpi_occupation_jour"),
           n("SELECT count() FROM gold_pilotage.fact_sejour"))
    r.egal("les sorties observées correspondent aux séjours clos dans la fenêtre",
           n("SELECT sum(nb_sorties) FROM gold_pilotage.kpi_occupation_jour"),
           n("""SELECT count() FROM gold_pilotage.fact_sejour
                WHERE est_en_cours = 0 AND date_sortie <=
                      (SELECT max(date_admission) FROM gold_pilotage.fact_sejour)"""))
    r.egal("jamais moins de présents que d'admis le même jour",
           n("""SELECT count() FROM gold_pilotage.kpi_occupation_jour
                WHERE nb_presents < nb_admissions"""), 0)
    # La série s'arrête au dernier jour de dépôt : au-delà, seuls les séjours
    # déjà admis subsisteraient et la courbe descendrait sans raison métier.
    r.egal("la série ne déborde pas la fenêtre d'observation",
           n("""SELECT count() FROM gold_pilotage.kpi_occupation_jour
                WHERE jour > (SELECT max(date_admission) FROM gold_pilotage.fact_sejour)"""), 0)

    # ⑧ Mortalité ----------------------------------------------------------
    r.titre("⑧ Mortalité hospitalière par service")
    for service, clos, deces, taux in lignes("""
            SELECT service, nb_sejours_clos, nb_deces, taux_pct
            FROM gold_pilotage.kpi_mortalite_service ORDER BY taux_pct DESC LIMIT 4"""):
        r.valeur(f"{service}", f"{taux:>5} %   {GRIS}{deces} décès sur {clos} séjours clos{RAZ}")
    _coherence_kpi(r, "kpi_mortalite_service")
    # Un séjour en cours n'a pas d'issue connue : le compter au dénominateur
    # reviendrait à le supposer vivant, et minorerait le taux d'autant.
    r.egal("dénominateur restreint aux séjours clos",
           n("SELECT sum(nb_sejours_clos) FROM gold_pilotage.kpi_mortalite_service"),
           n("SELECT countIf(est_en_cours = 0) FROM gold_pilotage.fact_sejour"))

    # ⑨ Case-mix -----------------------------------------------------------
    r.titre("⑨ Case-mix — pathologies principales par service")
    for service, pathologie, nb, part in lignes("""
            SELECT service, pathologie, nb_sejours, part_pct
            FROM gold_pilotage.kpi_casemix_service ORDER BY nb_sejours DESC LIMIT 4"""):
        r.valeur(f"{service} · {pathologie[:34]}", f"{part:>5} %   {GRIS}{nb} séjours{RAZ}")
    _coherence_kpi(r, "kpi_casemix_service")
    # La part est calculée SUR LE SERVICE : les parts d'un même service
    # somment à 100. Si elles sommaient à autre chose, le dénominateur de la
    # fenêtre analytique serait faux et chaque part serait fausse avec lui.
    r.egal("les parts d'un service somment à 100 %",
           n("""SELECT count() FROM (
                    SELECT service_code, round(sum(part_pct)) AS total
                    FROM gold_pilotage.kpi_casemix_service
                    GROUP BY service_code HAVING total != 100)"""), 0)
    r.egal("le case-mix couvre tous les séjours au diagnostic principal",
           n("SELECT sum(nb_sejours) FROM gold_pilotage.kpi_casemix_service"),
           n("""SELECT countIf(est_principal = 1) FROM gold_pilotage.fact_diagnostic"""))

    # ⑩ Origine géographique ------------------------------------------------
    # « Toute autre vue d'activité pertinente » (§4). C'est l'usage qui
    # justifie de conserver `region_code` jusqu'en gold (minimisation, §3) :
    # une donnée conservée sans usage est indéfendable, et cette vue est cet
    # usage — l'attractivité territoriale du CHU, par service.
    r.titre("⑩ Origine géographique des séjours, par service")
    for service, region, sejours, patients, part in lignes("""
            SELECT service, region_code, nb_sejours, nb_patients, part_pct
            FROM gold_pilotage.kpi_origine_service
            ORDER BY nb_sejours DESC LIMIT 4"""):
        r.valeur(f"{service} · dépt {region}",
                 f"{part:>5} %   {GRIS}{sejours} séjours, {patients} patients{RAZ}")
    _coherence_kpi(r, "kpi_origine_service")
    # Comme le case-mix, la part est calculée SUR LE SERVICE : les parts d'un
    # même service somment donc à 100.
    r.egal("les parts d'un service somment à 100 %",
           n("""SELECT count() FROM (
                    SELECT service_code, round(sum(part_pct)) AS total
                    FROM gold_pilotage.kpi_origine_service
                    GROUP BY service_code HAVING total != 100)"""), 0)
    r.egal("jamais plus de patients que de séjours",
           n("""SELECT count() FROM gold_pilotage.kpi_origine_service
                WHERE nb_patients > nb_sejours"""), 0)
    r.egal("aucun département de résidence vide",
           n("""SELECT count() FROM gold_pilotage.kpi_origine_service
                WHERE region_code = ''"""), 0)


# ── RGPD ─────────────────────────────────────────────────────────────────
IDENTIFIANTS_DIRECTS = ("nir", "nom", "prenom", "birth_date", "patient_id")
BASES = ("bronze", "silver", "gold_pilotage", "gold_recherche", "ops")
PERIMETRES = {
    "eds_pilotage": {"gold_pilotage"},
    "eds_recherche": {"gold_recherche"},
    "eds_exploitation": {"bronze", "silver", "ops", "quarantaine"},
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
    # Bronze porte l'horodatage d'ingestion, silver celui de construction ;
    # patients est une réduction du snapshot cumulatif, d'où le suffixe.
    # Les référentiels ne sont pas journaliers : rechargés en entier, non
    # partitionnés, sans _jour_depot — leur chemin de fichier porte le jour.
    for table, requises in (
        ("bronze.sejours",     {"_jour_depot", "_fichier_source", "_ingested_at", "_run_id"}),
        ("bronze.patients",    {"_jour_depot", "_fichier_source", "_ingested_at", "_run_id"}),
        ("bronze.monitoring",  {"_jour_depot", "_fichier_source", "_ingested_at", "_run_id"}),
        ("bronze.diagnostics", {"_jour_depot", "_fichier_source", "_ingested_at", "_run_id"}),
        ("bronze.ref_services", {"_fichier_source", "_ingested_at", "_run_id"}),
        ("bronze.ref_cim10",    {"_fichier_source", "_ingested_at", "_run_id"}),
        ("silver.sejours",     {"_jour_depot", "_fichier_source", "_built_at", "_run_id"}),
        ("silver.diagnostics", {"_jour_depot", "_fichier_source", "_built_at", "_run_id"}),
        ("silver.monitoring",  {"_jour_depot", "_fichier_source", "_built_at", "_run_id"}),
        ("silver.patients",    {"_jour_depot_retenu", "_fichier_source_retenu", "_built_at", "_run_id"}),
        ("quarantaine.rejets", {"_jour_depot", "_fichier_source", "_rejected_at", "_run_id"}),
    ):
        base, nom = table.split(".")
        colonnes = {x[0] for x in ch.query(f"""
            SELECT name FROM system.columns
            WHERE database = '{base}' AND table = '{nom}'""").result_rows}
        manquantes = sorted(requises - colonnes)
        r.controle(f"{table} porte son origine et son horodatage",
                   not manquantes, f"manque {manquantes}" if manquantes else "")

    # Une colonne déclarée ne suffit pas : elle doit être renseignée.
    for table, colonne in (("silver.sejours", "_fichier_source"),
                           ("quarantaine.rejets", "_fichier_source"),
                           ("silver.patients", "_fichier_source_retenu")):
        vides = int(ch.command(f"SELECT countIf({colonne} = '') FROM {table}"))
        r.controle(f"{table}.{colonne} renseigné partout",
                   vides == 0, f"{vides} ligne(s) sans provenance" if vides else "")
    # L'idempotence repose entièrement là-dessus : sans partition par jour,
    # rejouer un jour dupliquerait au lieu de remplacer.
    for table in ("sejours", "patients", "diagnostics", "monitoring"):
        cle = ch.command(f"""SELECT partition_key FROM system.tables
                             WHERE database = 'bronze' AND name = '{table}'""")
        r.controle(f"bronze.{table} partitionnée par jour de dépôt",
                   cle == "_jour_depot", f"clé = {cle!r}" if cle != "_jour_depot" else "")

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


# ── Conformité aux valeurs de référence ─────────────────────────────────
# Les autres sections vérifient des PROPRIÉTÉS de l'entrepôt (équations,
# intégrité, cohérence table <-> fait) : un contrôle peut passer sans que le
# chiffre publié soit celui attendu par l'intervenant. Celle-ci confronte
# directement l'entrepôt aux valeurs de référence fournies, contrôle par contrôle — c'est la
# seule section qui peut échouer sur une VALEUR juste alors que toutes les
# propriétés tiennent, et c'est voulu : les définitions de référence font foi.
_TOLERANCE_MOYENNE = 0.1


def _proche(obtenu: float, attendu: float, tolerance: float = _TOLERANCE_MOYENNE) -> bool:
    return abs(obtenu - attendu) <= tolerance


def conformite(r: Rapport) -> None:
    ref = _reference_complete()
    if ref is None:
        print("valeurs de référence absentes, section ignorée")
        return

    ch = client()
    n = lambda requete: int(ch.command(requete))
    ligne = lambda requete: ch.query(requete).result_rows

    r.titre("Silver — comptages exacts")
    ref_s = ref["silver"]
    r.egal("silver.patients", n("SELECT count() FROM silver.patients"), ref_s["patients"])
    r.egal("silver.sejours", n("SELECT count() FROM silver.sejours"), ref_s["sejours"])
    r.egal("sejours écartés (quarantaine, action='ecarte')",
           n("""SELECT count() FROM quarantaine.rejets
                WHERE source = 'sejours' AND action = 'ecarte'"""),
           ref_s["sejours_ecartes"])
    r.egal("silver.monitoring", n("SELECT count() FROM silver.monitoring"), ref_s["monitoring"])
    r.egal("monitoring écarté (quarantaine, action='ecarte')",
           n("""SELECT count() FROM quarantaine.rejets
                WHERE source = 'monitoring' AND action = 'ecarte'"""),
           ref_s["monitoring_ecartes"])

    r.titre("KPI 1 — DMS par service (comptage exact, moyennes ±0,1)")
    for row in ref["kpi1_dms_service"]:
        code = row["service_code"]
        trouve = ligne(f"""SELECT nb_sejours_clos, dms_jours, dms_heures
                           FROM gold_pilotage.kpi_dms_service
                           WHERE service_code = '{code}'""")
        if not trouve:
            r.controle(f"{code} : présent dans kpi_dms_service", False, "absent")
            continue
        nb, dms_j, dms_h = trouve[0]
        r.egal(f"{code} : nb_sejours", nb, row["nb_sejours"])
        r.controle(f"{code} : dms_jours ±0,1", _proche(dms_j, row["dms_jours"]),
                   f"{dms_j} (attendu {row['dms_jours']})")
        r.controle(f"{code} : dms_heures ±0,1", _proche(dms_h, row["dms_heures"]),
                   f"{dms_h} (attendu {row['dms_heures']})")

    r.titre("KPI 2 — Réadmission à 30 jours (définition brute, référence)")
    ref_2 = ref["kpi2_readmission"]
    num = n("SELECT sum(readmission_30j_brute) FROM gold_pilotage.fact_sejour")
    den = n("SELECT count() FROM gold_pilotage.fact_sejour")
    taux = round(100 * num / den, 2) if den else 0.0
    r.egal("réadmissions à 30 j (numérateur)", num, ref_2["nb_readmissions_30j"])
    r.egal("séjours (dénominateur)", den, ref_2["nb_sejours"])
    r.controle("taux ±0,1", _proche(taux, ref_2["taux_pct"]),
               f"{taux} (attendu {ref_2['taux_pct']})")

    r.titre("KPI 3 — Urgences par jour (comptages exacts, durée ±0,1)")
    for row in ref["kpi3_urgences_jour"]:
        jour = row["jour"]
        trouve = ligne(f"""SELECT nb_passages_urgences, nb_encore_presents, duree_moy_heures
                           FROM gold_pilotage.kpi_urgences_jour WHERE jour = '{jour}'""")
        if not trouve:
            r.controle(f"{jour} : présent dans kpi_urgences_jour", False, "absent")
            continue
        nb_p, nb_e, duree = trouve[0]
        r.egal(f"{jour} : nb_passages_urgences", nb_p, row["nb_passages"])
        r.egal(f"{jour} : nb_encore_presents", nb_e, row["nb_encore_presents"])
        r.controle(f"{jour} : duree_moy_heures ±0,1", _proche(duree, row["duree_moy_heures"]),
                   f"{duree} (attendu {row['duree_moy_heures']})")

    r.titre("KPI 4 — Relevés en alerte, par jour (somme de kpi_alertes_jour, exact)")
    for row in ref["kpi4_alertes_jour"]:
        jour = row["jour"]
        nb_r, nb_a = ligne(f"""SELECT sum(nb_releves), sum(nb_en_alerte)
                              FROM gold_pilotage.kpi_alertes_jour
                              WHERE jour = '{jour}'""")[0]
        nb_r, nb_a = nb_r or 0, nb_a or 0
        r.egal(f"{jour} : nb_releves", nb_r, row["nb_releves"])
        r.egal(f"{jour} : nb_alertes", nb_a, row["nb_alertes"])

    r.titre("KPI 5 — Prévalence (11 valeurs exactes, E84 et Q90 sous le seuil)")
    for row in ref["kpi5_prevalence"]:
        code = row["code_cim10"]
        trouve = ligne(f"""SELECT nb_patients FROM gold_recherche.coh_prevalence
                           WHERE code_cim10 = '{code}'""")
        if row["diffusable"]:
            if not trouve:
                r.controle(f"{code} : présent (attendu diffusable)", False, "absent")
            else:
                r.egal(f"{code} : nb_patients", trouve[0][0], row["nb_patients"])
        else:
            r.controle(f"{code} : absent (sous le seuil de 5 patients)", not trouve,
                       f"présent avec nb_patients={trouve[0][0]}" if trouve else "")

    r.titre("KPI 6 — Cohorte âge × sexe (89 cellules exactes, 13 sous le seuil)")
    echecs_avant = len(r.echecs)
    for code, tranche, sexe, attendu in ref["kpi6_cohorte_age_sexe"]:
        trouve = ligne(f"""SELECT nb_patients FROM gold_recherche.coh_description
                           WHERE code_cim10 = '{code}' AND tranche_age = '{tranche}'
                             AND sexe = '{sexe}'""")
        libelle = f"{code} {tranche} {sexe}"
        if attendu >= 5:
            r.egal(libelle, trouve[0][0] if trouve else 0, attendu)
        else:
            r.controle(f"{libelle} : absent (sous le seuil de 5 patients)", not trouve,
                       f"présent avec nb_patients={trouve[0][0]}" if trouve else "")
    if len(r.echecs) == echecs_avant:
        print(f"{VERT}102 cellules confrontées (89 attendues, 13 sous le seuil) : conforme{RAZ}")


SECTIONS = {"pseudonymisation": pseudonymisation, "qualite": qualite,
            "indicateurs": indicateurs, "rgpd": rgpd, "conformite": conformite}


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
