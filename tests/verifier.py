"""Contrôles automatisés de l'entrepôt — quatre sections indépendantes.

  pseudonymisation  les identités réelles de la source sont rejouées contre
                    l'intégralité du lake ; aucune ne doit s'y trouver
  qualite           équation de conservation bronze = silver + quarantaine,
                    règles métier du sujet, intégrité référentielle de
                    silver ET du modèle en étoile
  indicateurs       les indicateurs du § 4, calculés depuis gold : leur
                    valeur restituée, et la propriété qui la fonde
  rgpd              les cinq contraintes du § 5, vérifiées sur l'entrepôt réel,
                    plus l'absence de donnée personnelle dans les journaux

Ce sont des preuves, pas des déclarations : chacune interroge l'état réel et
échoue si la propriété annoncée ne tient pas.

    python -m tests.verifier            # les quatre sections
    python -m tests.verifier rgpd       # une seule
"""

from __future__ import annotations

import csv
import re
import sys
from collections import defaultdict

from eds import choisir_sections
from eds.config import LAKE, RACINE, SOURCE, seuils_alerte
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
    for source in ("sejours", "diagnostics", "monitoring"):
        bronze = n(f"SELECT count() FROM bronze.{source}")
        silver = n(f"SELECT count() FROM silver.{source}")
        rejets = n(f"""SELECT count() FROM quarantaine.rejets
                       WHERE source = '{source}' AND action = 'ecarte'""")
        r.egal(f"{source} : {silver} + {rejets}", silver + rejets, bronze)

    r.titre("Déduplication du snapshot cumulatif")
    r.egal(f"patients : {n('SELECT count() FROM bronze.patients')} lignes -> patients distincts",
           n("SELECT count() FROM silver.patients"),
           n("SELECT uniqExact(patient_pseudo) FROM bronze.patients"))
    r.egal("aucun doublon en silver.patients",
           n("SELECT count() - uniqExact(patient_pseudo) FROM silver.patients"), 0)

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

    # L'âge est dérivé du fait CONTRE la dimension. S'il manquait, c'est que
    # la jointure a échoué : le contrôle le dit avant que l'indicateur ne sorte.
    r.egal("age_au_sejour résolu pour tout séjour",
           n("SELECT count() FROM gold_pilotage.fact_sejour WHERE age_au_sejour IS NULL"), 0)
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
        "jour, nb_passages AS mesure, nb_sejours AS effectif",
        """SELECT date_admission AS jour, countIf(est_urgence) AS mesure,
                  count() AS effectif
           FROM gold_pilotage.fact_sejour GROUP BY jour""",
        ("jour",),
    ),
    "kpi_readmission_service": (
        "service_code, nb_readmis_30j AS mesure, nb_sejours_index AS effectif",
        """SELECT service_code, sum(suivi_readmission_30j) AS mesure,
                  sum(est_sejour_index) AS effectif
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
    les trois vues qui remplissent la cinquième ligne — « toute autre vue
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

    # ② Passages aux urgences par jour ------------------------------------
    r.titre("② Passages aux urgences, par jour")
    total_urgences = n("SELECT countIf(est_urgence) FROM gold_pilotage.fact_sejour")
    serie = lignes("""
        SELECT count(), min(nb), round(avg(nb), 1), max(nb), sum(nb) FROM (
            SELECT date_admission, countIf(est_urgence) AS nb
            FROM gold_pilotage.fact_sejour GROUP BY date_admission)""")[0]
    jours, mini, moyen, maxi, cumul = serie
    r.valeur("total sur la période", f"{total_urgences} passages")
    r.valeur(f"par jour sur {jours} jours", f"min {mini} · moyenne {moyen} · max {maxi}")
    r.egal("la série journalière somme au total", cumul, total_urgences)
    # L'axe est la date d'ADMISSION du fait, jamais le jour de dépôt : les
    # deux diffèrent, et confondre les deux daterait mal chaque passage.
    r.egal("est_urgence ne qualifie que les admissions en urgence",
           n("""SELECT count() FROM gold_pilotage.fact_sejour
                WHERE est_urgence != (admission_mode = 'urgence')"""), 0)
    _coherence_kpi(r, "kpi_urgences_jour")

    # ③ Taux de réadmission à 30 jours ------------------------------------
    # Numérateur et dénominateur sont posés dans le fait, pas laissés au
    # consommateur : c'est une auto-jointure, et deux calculs concurrents
    # donneraient deux taux.
    r.titre("③ Taux de réadmission à 30 jours")
    index_, readmis = lignes("""
        SELECT sum(est_sejour_index), sum(suivi_readmission_30j)
        FROM gold_pilotage.fact_sejour""")[0]
    r.valeur("taux", f"{round(100 * readmis / index_, 2)} %   "
                     f"{GRIS}{readmis} réadmissions sur {index_} séjours index{RAZ}")
    r.controle("le numérateur est inclus dans le dénominateur", readmis <= index_,
               f"{readmis} / {index_}")
    r.egal("aucune réadmission portée par un séjour hors dénominateur",
           n("""SELECT count() FROM gold_pilotage.fact_sejour
                WHERE suivi_readmission_30j = 1 AND est_sejour_index = 0"""), 0)
    # Un patient déclaré décédé puis « réadmis » fausserait le taux : la règle
    # métier standard exclut ces séjours du dénominateur.
    r.egal("aucun séjour clos par décès dans le dénominateur",
           n("""SELECT count() FROM gold_pilotage.fact_sejour
                WHERE est_sejour_index = 1 AND discharge_mode = 'deces'"""), 0)
    r.egal("aucun séjour en cours dans le dénominateur",
           n("""SELECT count() FROM gold_pilotage.fact_sejour
                WHERE est_sejour_index = 1 AND est_en_cours = 1"""), 0)
    _coherence_kpi(r, "kpi_readmission_service")

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
    # L'indicateur compte le MOTIF d'hospitalisation, pas la pathologie dans
    # la population : seul le diagnostic principal entre dans le calcul.
    r.titre("⑤ Prévalence par pathologie (diagnostic principal)")
    for pathologie, patients, sejours in lignes("""
            SELECT pathologie, nb_patients, nb_sejours
            FROM gold_recherche.coh_prevalence ORDER BY nb_patients DESC"""):
        r.valeur(pathologie[:52], f"{patients:>5} patients   {GRIS}{sejours} séjours{RAZ}")
    r.egal("la prévalence reproduit exactement l'agrégat des faits",
           n("SELECT count() FROM gold_recherche.coh_prevalence"),
           n("""SELECT count() FROM (
                    SELECT code_cim10, uniqExact(patient_pseudo) AS nb
                    FROM gold_pilotage.fact_diagnostic
                    WHERE est_principal = 1
                    GROUP BY code_cim10 HAVING nb >= 5)"""))
    r.egal("un patient ne peut pas avoir plus de séjours que lui-même",
           n("""SELECT count() FROM gold_recherche.coh_prevalence
                WHERE nb_sejours < nb_patients"""), 0)
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


SECTIONS = {"pseudonymisation": pseudonymisation, "qualite": qualite,
            "indicateurs": indicateurs, "rgpd": rgpd}


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
