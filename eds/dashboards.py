"""Définition déclarative des deux tableaux de bord.

Le SQL de chaque question est versionné ici : c'est l'« export » des
dashboards exigé par le rendu. Une remise à zéro de Metabase se répare par
`python -m eds.metabase`, sans reconstruire quoi que ce soit à la souris.

Chaque tableau de bord porte un encart de limites. Un indicateur dont on
n'énonce pas le périmètre n'est pas exploitable pour décider : c'est le
critère « fiabilité des indicateurs » du sujet.
"""
from __future__ import annotations

GRILLE = 24  # Metabase découpe la largeur d'un dashboard en 24 colonnes.

PILOTAGE = {
    "base": "pilotage",
    "nom": "Pilotage hospitalier",
    "description": "Activité, durées de séjour, qualité des soins et surveillance des constantes.",
    "encart": (
        "## Pilotage hospitalier\n"
        "Source : entrepôt EDS, couche `gold_pilotage` — modèle en étoile "
        "(`fact_sejour`, `fact_diagnostic`, `fact_releve` et leurs dimensions). "
        "Connexion cloisonnée : aucune donnée nominative, uniquement des pseudonymes.\n\n"
        "**Périmètres à connaître avant de décider :** la DMS ne porte que sur les "
        "séjours **clos** (1 190 séjours en cours sont exclus, faute de durée). Le taux "
        "de réadmission exclut les séjours dont le patient est **décédé**. La "
        "surveillance des constantes ne couvre que **Réanimation et Cardiologie**, "
        "seuls services équipés (≈ 40 % de leurs séjours) — les six autres n'ont aucun "
        "relevé.\n\n"
        "**Limite majeure :** l'historique disponible ne couvre que **3 jours**. Le taux "
        "de réadmission à 30 jours est donc un **plancher**, pas le taux réel."
    ),
    "questions": [
        {
            "nom": "Durée moyenne de séjour par service",
            "sql": """
                SELECT s.service                       AS "Service",
                       round(avg(f.duree_jours), 2)    AS "DMS (jours)",
                       count()                         AS "Séjours clos"
                FROM fact_sejour AS f
                INNER JOIN dim_service AS s ON f.service_code = s.service_code
                WHERE f.est_en_cours = 0
                GROUP BY s.service
                ORDER BY 2 DESC
            """,
            "affichage": "bar",
            "vis": {"graph.dimensions": ["Service"], "graph.metrics": ["DMS (jours)"],
                    "graph.y_axis.title_text": "Durée moyenne (jours)"},
            "pos": (0, 0, 12, 7),
        },
        {
            "nom": "Passages aux urgences par jour",
            "sql": """
                SELECT date_admission           AS "Jour",
                       countIf(est_urgence = 1) AS "Passages"
                FROM fact_sejour
                GROUP BY date_admission
                ORDER BY date_admission
            """,
            "affichage": "bar",
            "vis": {"graph.dimensions": ["Jour"], "graph.metrics": ["Passages"],
                    "graph.y_axis.title_text": "Passages"},
            "pos": (12, 0, 12, 7),
        },
        {
            "nom": "Taux de réadmission à 30 jours par service",
            "sql": """
                SELECT s.service                                AS "Service",
                       round(100.0 * sum(f.suivi_readmission_30j)
                                   / sum(f.est_sejour_index), 2) AS "Taux de réadmission (%)",
                       sum(f.est_sejour_index)                   AS "Séjours index"
                FROM fact_sejour AS f
                INNER JOIN dim_service AS s ON f.service_code = s.service_code
                WHERE f.est_sejour_index = 1
                GROUP BY s.service
                ORDER BY 2 DESC
            """,
            "affichage": "bar",
            "vis": {"graph.dimensions": ["Service"],
                    "graph.metrics": ["Taux de réadmission (%)"],
                    "graph.y_axis.title_text": "%"},
            "pos": (0, 7, 12, 7),
        },
        {
            "nom": "Relevés en alerte par jour — Réanimation et Cardiologie",
            "sql": """
                SELECT f.date_mesure            AS "Jour",
                       s.service                AS "Service",
                       countIf(f.en_alerte = 1) AS "Relevés en alerte"
                FROM fact_releve AS f
                INNER JOIN dim_service AS s ON f.service_code = s.service_code
                GROUP BY f.date_mesure, s.service
                ORDER BY f.date_mesure, s.service
            """,
            "affichage": "line",
            "vis": {"graph.dimensions": ["Jour", "Service"],
                    "graph.metrics": ["Relevés en alerte"],
                    "graph.y_axis.title_text": "Relevés en alerte"},
            "pos": (12, 7, 12, 7),
        },
        {
            # Croisement rendu possible par le modèle en étoile : il n'aurait
            # pas existé dans un entrepôt de KPI pré-agrégés, où il aurait
            # fallu anticiper cette combinaison précise.
            "nom": "Durée de séjour par service et tranche d'âge",
            "sql": """
                SELECT s.service                    AS "Service",
                       f.tranche_age                AS "Tranche d'âge",
                       round(avg(f.duree_jours), 2) AS "DMS (jours)",
                       count()                      AS "Séjours"
                FROM fact_sejour AS f
                INNER JOIN dim_service AS s ON f.service_code = s.service_code
                WHERE f.est_en_cours = 0 AND f.tranche_age != 'inconnu'
                GROUP BY s.service, f.tranche_age
                ORDER BY s.service, f.tranche_age
            """,
            "affichage": "table",
            "vis": {},
            "pos": (0, 14, 12, 8),
        },
        {
            "nom": "Détail des alertes par type",
            "sql": """
                SELECT f.date_mesure              AS "Jour",
                       s.service                  AS "Service",
                       count()                    AS "Relevés",
                       countIf(f.alerte_fc = 1)   AS "Fréq. cardiaque",
                       countIf(f.alerte_spo2 = 1) AS "Saturation",
                       countIf(f.alerte_temp = 1) AS "Température",
                       round(100.0 * countIf(f.en_alerte = 1) / count(), 2)
                                                  AS "Taux d'alerte (%)"
                FROM fact_releve AS f
                INNER JOIN dim_service AS s ON f.service_code = s.service_code
                GROUP BY f.date_mesure, s.service
                ORDER BY f.date_mesure, s.service
            """,
            "affichage": "table",
            "vis": {},
            "pos": (12, 14, 12, 8),
        },
    ],
}

RECHERCHE = {
    "base": "recherche",
    "nom": "Recherche clinique",
    "description": "Prévalence des pathologies et description des cohortes, sous contrainte RGPD.",
    "encart": (
        "## Recherche clinique\n"
        "Source : entrepôt EDS, couche `gold_recherche` — agrégats dérivés du modèle "
        "en étoile. Connexion cloisonnée — "
        "**cette base est distincte de celle du pilotage** et n'y donne aucun accès.\n\n"
        "**Garanties d'anonymisation, appliquées à l'écriture des données :**\n"
        "- l'identifiant patient est un **pseudonyme** (HMAC-SHA256 salé, non réversible) "
        "et n'est pas exposé ici ;\n"
        "- l'âge n'est diffusé qu'en **tranches de 10 ans**, jamais à l'année. Mesuré sur "
        "ces données : l'année exacte rendait 102 patients uniques sur (année, sexe, "
        "région) ; les tranches portent **100 % de la population à k ≥ 5** ;\n"
        "- **aucune cohorte de moins de 5 patients** n'existe dans cette base — le filtre "
        "est appliqué au calcul, il n'y a rien à oublier de masquer.\n\n"
        "**Limite :** 3 jours d'observation. Les prévalences reflètent la file active de "
        "cette période, non l'épidémiologie de l'établissement."
    ),
    "questions": [
        {
            "nom": "Prévalence par pathologie",
            "sql": """
                SELECT pathologie AS "Pathologie",
                       nb_patients AS "Patients",
                       nb_sejours AS "Séjours"
                FROM coh_prevalence
                ORDER BY nb_patients DESC
            """,
            "affichage": "row",
            "vis": {"graph.dimensions": ["Pathologie"], "graph.metrics": ["Patients"]},
            "pos": (0, 0, 12, 8),
        },
        {
            "nom": "Distribution par tranche d'âge et sexe",
            "sql": """
                SELECT tranche_age AS "Tranche d'âge",
                       sexe AS "Sexe",
                       sum(nb_patients) AS "Patients"
                FROM coh_description
                GROUP BY tranche_age, sexe
                ORDER BY tranche_age
            """,
            "affichage": "bar",
            "vis": {"graph.dimensions": ["Tranche d'âge", "Sexe"],
                    "graph.metrics": ["Patients"],
                    "stackable.stack_type": "stacked",
                    "graph.y_axis.title_text": "Patients"},
            "pos": (12, 0, 12, 8),
        },
        {
            "nom": "Cohortes détaillées (toutes ≥ 5 patients)",
            "sql": """
                SELECT pathologie AS "Pathologie",
                       tranche_age AS "Tranche d'âge",
                       sexe AS "Sexe",
                       nb_patients AS "Patients"
                FROM coh_description
                ORDER BY nb_patients DESC
            """,
            "affichage": "table",
            "vis": {},
            "pos": (0, 8, 24, 8),
        },
    ],
}

TABLEAUX = (PILOTAGE, RECHERCHE)
