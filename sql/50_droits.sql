-- ─────────────────────────────────────────────────────────────────────────
-- CLOISONNEMENT DES DROITS — contrainte RGPD du §5.
--
-- « Pilotage et recherche ne voient pas les mêmes données
--   -> droits d'accès distincts. »
--
-- Deux comptes, chacun avec le droit de lecture sur UNE SEULE base gold.
-- Ni l'un ni l'autre n'a accès à bronze ni à silver : ils ne peuvent donc
-- pas remonter aux données de détail, ni recomposer une cohorte sous le
-- seuil des 5 patients à partir des lignes brutes.
--
-- Le refus est prononcé par le MOTEUR. Ce n'est pas une convention
-- applicative que l'on pourrait contourner en écrivant une autre requête.
-- ─────────────────────────────────────────────────────────────────────────

-- Chaque compte est DROP puis recréé, plutôt que `CREATE USER IF NOT EXISTS` :
-- ce script n'exécute que des GRANT, jamais de REVOKE, et ClickHouse ne
-- réinitialise pas les droits d'un utilisateur déjà existant. Un GRANT retiré
-- d'ici (ou une colonne renommée en amont) laisserait donc un droit fantôme
-- accroché au compte, invisible tant qu'on ne compare pas `system.grants` au
-- schéma courant. Repartir d'un compte neuf à chaque exécution garantit que
-- les droits vivants sont exactement ceux que ce fichier décrit — ni plus,
-- ni un résidu d'une itération passée.
DROP USER IF EXISTS eds_pilotage;
CREATE USER eds_pilotage
    IDENTIFIED WITH sha256_password BY '{mdp_pilotage}';

DROP USER IF EXISTS eds_recherche;
CREATE USER eds_recherche
    IDENTIFIED WITH sha256_password BY '{mdp_recherche}';

-- ── Compte de pilotage : droits AU NIVEAU COLONNE ───────────────────────
--
-- Un GRANT sur la base entière donnerait accès à `patient_pseudo` et au
-- grain du séjour, très au-delà du besoin : la direction consulte des
-- indicateurs d'activité, elle n'a jamais à désigner un patient.
--
-- Les droits sont donc limités aux seules colonnes que ses indicateurs
-- utilisent. Ni `patient_pseudo`, ni `stay_id`, ni les horodatages précis,
-- ni les constantes brutes n'y figurent. `dim_patient` et `fact_diagnostic`
-- ne sont pas accordées du tout.
--
-- Conséquence : même si ce compte parvenait à composer une requête, il ne
-- pourrait pas dénombrer de patients ni relier deux séjours entre eux.
GRANT SELECT(
    service_code, date_admission, tranche_age,
    duree_jours, est_en_cours, est_urgence,
    est_sejour_index, suivi_readmission_30j, readmission_30j_brute
) ON gold_pilotage.fact_sejour TO eds_pilotage;

GRANT SELECT(
    service_code, date_mesure,
    alerte_fc, alerte_spo2, alerte_temp, en_alerte
) ON gold_pilotage.fact_releve TO eds_pilotage;

-- ── Les indicateurs agrégés : accordés en entier ────────────────────────
--
-- Ces huit tables ne portent NI pseudonyme, NI stay_id, NI horodatage
-- précis — rien qui désigne un patient ou un séjour. `kpi_origine_service`
-- non plus : elle croise service et département de résidence, jamais le
-- patient lui-même. Le découpage colonne par colonne n'a donc pas d'objet
-- ici : il n'y a aucune colonne à retenir.
--
-- C'est le chemin de lecture normal du pilotage. L'accès au grain de
-- l'événement ci-dessus subsiste pour les analyses qu'aucune table figée ne
-- couvre — croiser la DMS par service ET par tranche d'âge, par exemple.
GRANT SELECT ON gold_pilotage.kpi_dms_service          TO eds_pilotage;
GRANT SELECT ON gold_pilotage.kpi_urgences_jour        TO eds_pilotage;
GRANT SELECT ON gold_pilotage.kpi_readmission_service  TO eds_pilotage;
GRANT SELECT ON gold_pilotage.kpi_alertes_jour         TO eds_pilotage;
GRANT SELECT ON gold_pilotage.kpi_occupation_jour      TO eds_pilotage;
GRANT SELECT ON gold_pilotage.kpi_mortalite_service    TO eds_pilotage;
GRANT SELECT ON gold_pilotage.kpi_casemix_service      TO eds_pilotage;
GRANT SELECT ON gold_pilotage.kpi_origine_service      TO eds_pilotage;

GRANT SELECT(service_code, service) ON gold_pilotage.dim_service TO eds_pilotage;

-- ── Compte recherche ────────────────────────────────────────────────────
-- Les deux tables de cohortes sont déjà agrégées, filtrées à k >= 5, et ne
-- contiennent aucun pseudonyme. Les droits restent néanmoins limités aux
-- colonnes exposées, par cohérence.
GRANT SELECT(code_cim10, pathologie, nb_patients, nb_patients_principal, nb_sejours)
    ON gold_recherche.coh_prevalence TO eds_recherche;

GRANT SELECT(code_cim10, pathologie, tranche_age, sexe, nb_patients)
    ON gold_recherche.coh_description TO eds_recherche;


-- ── Compte d'exploitation ───────────────────────────────────────────────
-- L'administrateur a besoin d'investiguer sur les couches techniques —
-- retrouver la ligne d'origine d'un incident, préparer une piste d'audit,
-- instruire une demande d'effacement.
--
-- Il n'utilise PAS pour cela le compte du pipeline : celui-ci peut créer et
-- supprimer des bases, ce qui n'a pas sa place derrière une interface web.
-- Un compte distinct, en LECTURE SEULE sur les couches techniques, applique
-- le principe de moindre privilège : aucune écriture n'est possible avec ce
-- compte, quelle que soit la requête saisie.
DROP USER IF EXISTS eds_exploitation;
CREATE USER eds_exploitation
    IDENTIFIED WITH sha256_password BY '{mdp_exploitation}';

GRANT SELECT ON bronze.*      TO eds_exploitation;
GRANT SELECT ON silver.*      TO eds_exploitation;
GRANT SELECT ON ops.*         TO eds_exploitation;

-- La quarantaine est une base à part, donc un GRANT à part : c'est
-- précisément ce que permet de l'avoir sortie de silver. Le jour où sa
-- rétention diffère de celle de l'entrepôt, ou bien où l'on veut la confier à
-- un référent qualité sans lui ouvrir silver, rien d'autre n'est à toucher.
GRANT SELECT ON quarantaine.* TO eds_exploitation;
