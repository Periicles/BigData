-- ═══════════════════════════════════════════════════════════════════════
-- Construction de la couche silver depuis bronze.
-- Recalcul intégral : chaque table est vidée puis reconstruite.
-- Le marqueur {run_id} est substitué par le pipeline.
--
-- CE QUE FAIT CETTE COUCHE, ET RIEN D'AUTRE : appliquer les règles de
-- VALIDITÉ que le sujet fournit, tracer chaque exclusion, enrichir depuis
-- les référentiels. Les règles métier paramétrables — seuils d'alerte, âge
-- à l'événement — sont en gold : voir l'en-tête de 20_silver.sql.
--
-- PLAGES DE PLAUSIBILITÉ (§3, fournies par le sujet). Un relevé qui en sort
-- n'est pas une alerte, c'est une donnée invalide :
--     FC 20–250 bpm · SpO2 50–100 % · température 30–45 °C
--
-- CONTRÔLES DU §3 DU SUJET, un par un :
--   patients    doublons -> déduplication (snapshot cumulatif)
--   patients    sexe normalisé M/F -> sinon 'inconnu', ligne CORRIGÉE
--   patients    dates valides -> une date de naissance illisible CORRIGE
--               birth_year en NULL, la ligne reste (attribut descriptif)
--   patients    patient_id vide -> pseudonyme vide -> ÉCARTÉE
--   sejours     discharge_ts < admission_ts -> ÉCARTÉE
--   sejours     discharge_ts vide = séjour en cours -> conservé
--   sejours     dates valides -> une date illisible ÉCARTE la ligne
--   sejours     patient_id vide -> pseudonyme vide -> ÉCARTÉE
--   monitoring  plages physiologiques -> ÉCARTÉE
--
-- RÈGLE DÉRIVÉE, au-delà de l'énumération littérale du §3 : un relevé de
-- monitoring dont l'horodatage tombe hors de la fenêtre de son séjour (avant
-- l'admission, ou après la sortie d'un séjour CLOS) ÉCARTE le relevé, motif
-- 'releve_hors_sejour' — cf. section MONITORING plus bas pour le détail et
-- l'ordre vis-à-vis de 'capteur_hors_plage' et 'sejour_ecarte'. Cette fenêtre
-- n'a de sens que pour un séjour COHÉRENT (cf. paragraphe suivant) : sur un
-- séjour dont la sortie précède l'admission, on ne sait pas laquelle des deux
-- dates est fautive, donc on ne sait pas ce que serait « avant » ou « après ».
--
-- DÉCISION DE L'INTERVENANT — LA COHÉRENCE TEMPORELLE D'UN SÉJOUR N'ÉCARTE
-- NI SES DIAGNOSTICS NI SES RELEVÉS. Un séjour dont `discharge_ts <
-- admission_ts` est écarté de `silver.sejours` (règle du §3, ci-dessus) :
-- l'anomalie porte sur DEUX COLONNES DE SÉJOUR. Elle ne dit rien de la
-- validité des codes CIM-10 posés pendant ce séjour, ni des mesures prises
-- au chevet du patient — ce sont des faits médicaux distincts, à une autre
-- table. `diagnostics` et `monitoring` lisent donc `bronze.sejours`
-- directement (version retenue après déduplication, jamais `silver.sejours`)
-- pour s'enrichir du patient, du service et de l'admission porteurs, et ne
-- s'appuient sur `silver.sejours` que pour poser un drapeau `sejour_coherent`
-- — jamais pour filtrer. Seule l'absence de patient identifié (séjour
-- introuvable en bronze, motif 'sejour_inconnu' ; ou patient_pseudo vide,
-- motif 'sejour_ecarte') écarte réellement un diagnostic ou un relevé — cf.
-- les sections DIAGNOSTICS et MONITORING plus bas.
--
-- Toute ligne fautive part en `quarantaine.rejets` avec son motif et son
-- issue, de sorte que  bronze = silver + quarantaine('ecarte')  à la ligne
-- près, les corrections étant tracées sans être soustraites.
-- ═══════════════════════════════════════════════════════════════════════
TRUNCATE TABLE quarantaine.rejets;

-- ── 1. PATIENTS ─────────────────────────────────────────────────────────
-- `patients` est un SNAPSHOT CUMULATIF : chaque fichier journalier contient
-- toute la population connue à date (18 000 lignes pour 6 000 patients).
-- On retient la version du jour de dépôt le plus récent — un rang par
-- patient_pseudo, réutilisé par les trois contrôles ci-dessous, pour que la
-- version qu'ils examinent soit la même partout.
--
-- POURQUOI UN row_number() ET NON UN argMax(birth_year, _jour_depot).
-- `birth_year` est Nullable (date de naissance illisible en source). Or
-- argMax IGNORE les lignes dont l'ARGUMENT est NULL : vérifié empiriquement
-- sur ClickHouse 25.8, argMax(v, t) avec la ligne au t maximal portant v
-- NULL renvoie la valeur v d'une ligne plus ANCIENNE si elle existe, jamais
-- NULL. Un patient dont la dernière version a `birth_year` illisible
-- « retomberait » ainsi sur une année de naissance qui n'est plus celle du
-- dépôt retenu — silencieusement. row_number() n'a pas ce défaut : il classe
-- la LIGNE entière par _jour_depot, sans distinguer ses colonnes, et NULL
-- reste NULL si c'est la valeur de la ligne au rang 1.
TRUNCATE TABLE silver.patients;

-- Patient manquant : patient_id vide en source, la pseudonymisation ne
-- produit pas de hachage bidon (cf. eds/lake.py `pseudonymiser`) — un
-- pseudonyme vide agrégerait, au grain de patient_pseudo, TOUTES les lignes
-- sans identifiant sous UNE fausse personne à N séjours, qui gonflerait la
-- réadmission. Motif mutuellement exclusif des deux suivants : ceux-ci ne
-- portent que sur `patient_pseudo != ''`.
--
-- Une seule ligne de quarantaine résume le groupe entier : l'équation de
-- conservation de `patients` n'est pas ligne à ligne (snapshot cumulatif,
-- cf. tests.verifier qualite), tracer chaque ligne bronze fusionnée
-- compterait plusieurs fois la même exclusion.
INSERT INTO
    quarantaine.rejets
SELECT
    'patients',
    '(vide)',
    'patient_manquant',
    'ecarte',
    concat(toString(count()), ' ligne(s) bronze fusionnées sous un pseudonyme vide'),
    max(_jour_depot),
    argMax(_fichier_source, _jour_depot),
    '{run_id}',
    now()
FROM
    bronze.patients
WHERE
    patient_pseudo = ''
HAVING
    count() > 0;

-- Règle du sujet : « sexe normalisé (M/F) ». La casse et les espaces sont
-- redressés ; toute autre valeur devient 'inconnu'.
--
-- Décision documentée : on CORRIGE, on n'écarte pas. Écarter le patient
-- orphelinerait tous ses séjours pour un attribut purement descriptif, qui
-- n'entre dans aucune clé. La ligne est donc conservée et signalée.
INSERT INTO
    quarantaine.rejets
SELECT
    'patients',
    patient_pseudo,
    'sexe_non_normalise',
    'corrige',
    concat('sexe source [', sex, '] -> inconnu'),
    _jour_depot,
    _fichier_source,
    '{run_id}',
    now()
FROM
    (
        SELECT
            *,
            row_number() OVER (
                PARTITION BY patient_pseudo
                ORDER BY _jour_depot DESC
            ) AS rang
        FROM
            bronze.patients
        WHERE
            patient_pseudo != ''
    )
WHERE
    rang = 1
    AND upper(trim(sex)) NOT IN ('M', 'F');

-- Règle du sujet : « dates valides ». Une date de naissance illisible en
-- source (birth_year NULL, cf. eds/lake.py) suit le même principe que le
-- sexe hors nomenclature : c'est un attribut DESCRIPTIF, il n'entre dans
-- aucune clé. Écarter le patient orphelinerait tous ses séjours pour une
-- seule colonne manquante — on CORRIGE (birth_year reste NULL) et on trace.
INSERT INTO
    quarantaine.rejets
SELECT
    'patients',
    patient_pseudo,
    'date_naissance_illisible',
    'corrige',
    'date de naissance illisible en source -> birth_year NULL',
    _jour_depot,
    _fichier_source,
    '{run_id}',
    now()
FROM
    (
        SELECT
            *,
            row_number() OVER (
                PARTITION BY patient_pseudo
                ORDER BY _jour_depot DESC
            ) AS rang
        FROM
            bronze.patients
        WHERE
            patient_pseudo != ''
    )
WHERE
    rang = 1
    AND birth_year IS NULL;

INSERT INTO
    silver.patients
SELECT
    patient_pseudo,
    birth_year,
    if(upper(trim(sex)) IN ('M', 'F'), upper(trim(sex)), 'inconnu'),
    region_code,
    _jour_depot,
    _fichier_source,
    '{run_id}',
    now()
FROM
    (
        SELECT
            *,
            row_number() OVER (
                PARTITION BY patient_pseudo
                ORDER BY _jour_depot DESC
            ) AS rang
        FROM
            bronze.patients
        WHERE
            patient_pseudo != ''
    )
WHERE
    rang = 1;

-- ── 2. SÉJOURS ──────────────────────────────────────────────────────────
-- Règle du sujet : écarter si discharge_ts < admission_ts.
-- Règle du sujet : discharge_ts vide = séjour EN COURS, légitime, conservé.
-- Règle du sujet : « dates valides ».
-- Décision documentée : discharge_mode vide est normalisé en 'inconnu' plutôt
--   qu'écarté — la durée reste calculable. Sur ce dépôt, seuls les 683 séjours
--   EN COURS sont concernés ; aucun séjour clos n'est privé de mode de sortie.
--
-- L'ordre compte : un patient manquant est écarté D'ABORD, puis une date
-- illisible est écartée AVANT l'incohérence temporelle, sinon la comparaison
-- porterait sur un NULL et la ligne échapperait aux deux contrôles. Les TROIS
-- motifs sont donc mutuellement exclusifs, et l'équation de conservation ne
-- double-compte aucune ligne.
--
-- Patient manquant : patient_id vide en source, pseudonyme vide (cf.
-- eds/lake.py). Un séjour rattaché à un pseudonyme vide ÉCARTE le séjour
-- lui-même — sans lui, ses diagnostics et relevés suivent via le motif
-- 'sejour_ecarte', déjà en place plus bas.
INSERT INTO
    quarantaine.rejets
SELECT
    'sejours',
    stay_id,
    'patient_manquant',
    'ecarte',
    'patient_pseudo vide (patient_id absent en source)',
    _jour_depot,
    _fichier_source,
    '{run_id}',
    now()
FROM
    bronze.sejours
WHERE
    patient_pseudo = '';

INSERT INTO
    quarantaine.rejets
SELECT
    'sejours',
    stay_id,
    'date_illisible',
    'ecarte',
    if(
        admission_ts IS NULL,
        'date d''admission illisible dans la source',
        'date de sortie non vide et illisible dans la source'
    ),
    _jour_depot,
    _fichier_source,
    '{run_id}',
    now()
FROM
    bronze.sejours
WHERE
    patient_pseudo != ''
    AND (
        admission_ts IS NULL
        OR _discharge_illisible = 1
    );

INSERT INTO
    quarantaine.rejets
SELECT
    'sejours',
    stay_id,
    'incoherence_temporelle',
    'ecarte',
    concat(
        'admission=',
        toString(admission_ts),
        ' sortie=',
        toString(discharge_ts)
    ),
    _jour_depot,
    _fichier_source,
    '{run_id}',
    now()
FROM
    bronze.sejours
WHERE
    patient_pseudo != ''
    AND admission_ts IS NOT NULL
    AND _discharge_illisible = 0
    AND discharge_ts IS NOT NULL
    AND discharge_ts < admission_ts;

TRUNCATE TABLE silver.sejours;

INSERT INTO
    silver.sejours
SELECT
    s.stay_id,
    s.patient_pseudo,
    s.service_code,
    coalesce(r.service_label, 'inconnu'),
    assumeNotNull(s.admission_ts),
    s.discharge_ts,
    s.admission_mode,
    if(
        s.discharge_mode = '',
        'inconnu',
        s.discharge_mode
    ),
    if(
        s.discharge_ts IS NULL,
        NULL,
        dateDiff('minute', s.admission_ts, s.discharge_ts) / 1440.0
    ),
    s.discharge_ts IS NULL,
    s._jour_depot,
    s._fichier_source,
    '{run_id}',
    now()
FROM
    bronze.sejours AS s
    LEFT JOIN bronze.ref_services AS r ON s.service_code = r.service_code
WHERE
    s.patient_pseudo != ''
    AND s.admission_ts IS NOT NULL
    AND s._discharge_illisible = 0
    AND (
        s.discharge_ts IS NULL
        OR s.discharge_ts >= s.admission_ts
    );

-- ── 3. DIAGNOSTICS ──────────────────────────────────────────────────────
-- Aucune anomalie propre : intégrité référentielle intégralement vérifiée
-- (0 code hors nomenclature, 0 séjour orphelin, 1 principal par séjour).
--
-- Un diagnostic est conservé SI ET SEULEMENT SI son séjour porteur existe
-- dans `bronze.sejours` (version retenue après déduplication) avec un
-- `patient_pseudo` NON VIDE — cf. l'en-tête de ce fichier pour pourquoi la
-- cohérence temporelle du séjour n'entre pas dans ce test. Deux motifs
-- d'exclusion, MUTUELLEMENT EXCLUSIFS (le premier porte sur les stay_id
-- absents, le second sur les stay_id présents mais sans patient) :
--   'sejour_inconnu' — stay_id absent de bronze.sejours : aucune trace du
--                       séjour porteur, quel qu'il soit.
--   'sejour_ecarte'  — stay_id présent, mais patient_pseudo vide (patient_id
--                       absent en source) : sans patient, le diagnostic ne
--                       peut être rattaché à personne.
-- Les deux valent 0 ligne sur ce dépôt (12 720 diagnostics, aucun écart) —
-- démontré par injection, cf. tests.demontrer qualite.
INSERT INTO
    quarantaine.rejets
SELECT
    'diagnostics',
    concat(stay_id, '/', code_cim10),
    'sejour_inconnu',
    'ecarte',
    'stay_id absent de bronze.sejours',
    _jour_depot,
    _fichier_source,
    '{run_id}',
    now()
FROM
    bronze.diagnostics
WHERE
    stay_id NOT IN (
        SELECT
            stay_id
        FROM
            bronze.sejours
    );

INSERT INTO
    quarantaine.rejets
SELECT
    'diagnostics',
    concat(d.stay_id, '/', d.code_cim10),
    'sejour_ecarte',
    'ecarte',
    'séjour porteur sans patient identifié (patient_pseudo vide)',
    d._jour_depot,
    d._fichier_source,
    '{run_id}',
    now()
FROM
    bronze.diagnostics AS d
    INNER JOIN (
        SELECT
            *
        FROM
            (
                SELECT
                    *,
                    row_number() OVER (
                        PARTITION BY stay_id
                        ORDER BY _jour_depot DESC
                    ) AS rang
                FROM
                    bronze.sejours
            )
        WHERE
            rang = 1
    ) AS s ON d.stay_id = s.stay_id
WHERE
    s.patient_pseudo = '';

TRUNCATE TABLE silver.diagnostics;

INSERT INTO
    silver.diagnostics
SELECT
    d.stay_id,
    d.code_cim10,
    d.type_diag,
    coalesce(c.libelle, 'inconnu'),
    s.patient_pseudo,
    s.service_code,
    s.admission_ts,
    -- Cohérent = présent dans silver.sejours. Un LEFT JOIN, pas un IN : il
    -- réutilise ci-dessous la même jointure pour poser le drapeau, sans
    -- sous-requête corrélée supplémentaire. Le test est `!= ''`, PAS
    -- `IS NOT NULL` : `join_use_nulls` vaut 0 sur ce serveur (défaut
    -- ClickHouse), un LEFT JOIN sans correspondance remplit donc `stay_id`
    -- (String, non-Nullable) avec la valeur PAR DÉFAUT du type — la chaîne
    -- vide — jamais NULL. Même piège, même parade que pour `dim_patient`
    -- plus bas (31_gold_transform.sql) : `IS NOT NULL` y serait toujours
    -- vrai, et `sejour_coherent` vaudrait 1 partout.
    (sj.stay_id != ''),
    d._jour_depot,
    d._fichier_source,
    '{run_id}',
    now()
FROM
    bronze.diagnostics AS d
    INNER JOIN (
        SELECT
            *
        FROM
            (
                SELECT
                    *,
                    row_number() OVER (
                        PARTITION BY stay_id
                        ORDER BY _jour_depot DESC
                    ) AS rang
                FROM
                    bronze.sejours
            )
        WHERE
            rang = 1
    ) AS s ON d.stay_id = s.stay_id
    LEFT JOIN bronze.ref_cim10 AS c ON d.code_cim10 = c.code_cim10
    LEFT JOIN silver.sejours AS sj ON d.stay_id = sj.stay_id
WHERE
    s.patient_pseudo != '';

-- ── 4. MONITORING ───────────────────────────────────────────────────────
-- Règle du sujet : écarter hors plage physiologique.
--
-- CONSTAT : FC et SpO2 sont TOUJOURS aberrantes ensemble (858 fois les
-- deux, 0 fois l'une seule), sur 4 combinaisons de butée — (0|500) × (0|120).
-- Ce n'est pas du bruit de mesure mais un CAPTEUR DÉCONNECTÉ. Le relevé
-- entier est écarté : un capteur en panne ne garantit la fiabilité d'aucune
-- de ses mesures, et ne conserver que la température créerait des relevés
-- partiels au grain incohérent.
INSERT INTO
    quarantaine.rejets
SELECT
    'monitoring',
    concat(stay_id, '@', toString(ts)),
    'capteur_hors_plage',
    'ecarte',
    concat(
        'fc=',
        toString(heart_rate),
        ' spo2=',
        toString(spo2),
        ' temp=',
        toString(temp_c)
    ),
    _jour_depot,
    _fichier_source,
    '{run_id}',
    now()
FROM
    bronze.monitoring
WHERE
    heart_rate NOT BETWEEN 20
    AND 250
    OR spo2 NOT BETWEEN 50
    AND 100
    OR temp_c NOT BETWEEN 30
    AND 45;

-- Le séjour porteur, version retenue de bronze.sejours — même principe que
-- pour les diagnostics ci-dessus, et pour la même raison : gold n'a plus
-- besoin de lire silver.sejours pour fact_releve.
--
-- Deux motifs, MUTUELLEMENT EXCLUSIFS l'un de l'autre (même partition que
-- pour les diagnostics : stay_id absent vs. stay_id présent sans patient),
-- et tous deux restreints aux relevés physiologiquement PLAUSIBLES : un
-- relevé hors plage est déjà absorbé par 'capteur_hors_plage' ci-dessus, quel
-- que soit le séjour auquel il est rattaché — sans quoi il serait tracé deux
-- fois. Les deux valent 0 ligne sur ce dépôt — démontré par injection, cf.
-- tests.demontrer qualite.
INSERT INTO
    quarantaine.rejets
SELECT
    'monitoring',
    concat(stay_id, '@', toString(ts)),
    'sejour_inconnu',
    'ecarte',
    'stay_id absent de bronze.sejours',
    _jour_depot,
    _fichier_source,
    '{run_id}',
    now()
FROM
    bronze.monitoring
WHERE
    stay_id NOT IN (
        SELECT
            stay_id
        FROM
            bronze.sejours
    )
    AND heart_rate BETWEEN 20
    AND 250
    AND spo2 BETWEEN 50
    AND 100
    AND temp_c BETWEEN 30
    AND 45;

INSERT INTO
    quarantaine.rejets
SELECT
    'monitoring',
    concat(m.stay_id, '@', toString(m.ts)),
    'sejour_ecarte',
    'ecarte',
    'séjour porteur sans patient identifié (patient_pseudo vide)',
    m._jour_depot,
    m._fichier_source,
    '{run_id}',
    now()
FROM
    bronze.monitoring AS m
    INNER JOIN (
        SELECT
            *
        FROM
            (
                SELECT
                    *,
                    row_number() OVER (
                        PARTITION BY stay_id
                        ORDER BY _jour_depot DESC
                    ) AS rang
                FROM
                    bronze.sejours
            )
        WHERE
            rang = 1
    ) AS s ON m.stay_id = s.stay_id
WHERE
    m.heart_rate BETWEEN 20
    AND 250
    AND m.spo2 BETWEEN 50
    AND 100
    AND m.temp_c BETWEEN 30
    AND 45
    AND s.patient_pseudo = '';

-- Règle dérivée (au-delà de la liste littérale du §3) : un relevé dont
-- l'horodatage tombe hors de la présence réelle du patient est une donnée
-- invalide, pas une alerte. Cette fenêtre n'a de sens QUE pour un séjour
-- COHÉRENT (présent dans silver.sejours) : sur un séjour dont la sortie
-- précède l'admission, on ne sait pas laquelle des deux dates fait foi, donc
-- « avant l'admission » et « après la sortie » ne veulent rien dire — cf.
-- l'en-tête de ce fichier, DÉCISION DE L'INTERVENANT. Le `INNER JOIN
-- silver.sejours` ci-dessous porte exactement cette restriction : il ne voit
-- que les séjours cohérents, jamais les autres, qui n'entrent donc jamais
-- dans ce contrôle — quel que soit leur motif d'exclusion de silver.sejours.
--
-- ORDRE ET EXCLUSIVITÉ MUTUELLE avec les trois motifs qui précèdent :
--   1. 'capteur_hors_plage' est vérifié EN PREMIER et absorbe tout relevé
--      physiologiquement aberrant, quel que soit le séjour auquel il est
--      rattaché.
--   2. 'sejour_inconnu' et 'sejour_ecarte' absorbent ensuite les relevés
--      physiologiquement plausibles dont le séjour porteur n'a pas de
--      patient identifié (absent de bronze.sejours, ou patient_pseudo vide).
--   3. 'releve_hors_sejour' (ici) ne porte donc que sur les relevés
--      physiologiquement plausibles, RATTACHÉS À UN PATIENT, ET dont le
--      séjour est COHÉRENT, dont l'horodatage tombe hors de sa fenêtre :
--      `ts < admission_ts`, ou `discharge_ts` non NULL et `ts >
--      discharge_ts` — un séjour EN COURS n'a pas de borne haute, aucun
--      relevé n'y est donc écarté à ce titre.
--
-- SUR CE DÉPÔT, ce motif n'attrape aucune ligne. L'exploration
-- (RAPPORT-EXPLORATION.md) signale 528 relevés postérieurs à une sortie ;
-- ils sont tous rattachés aux 68 séjours à incohérence temporelle
-- (`discharge_ts < admission_ts`). Avant la présente décision, ils étaient
-- écartés avec leur séjour (520 par l'ancien motif générique
-- 'sejour_ecarte', et 8, également hors plage physiologique, par
-- 'capteur_hors_plage', qui primait) ; ils sont désormais CONSERVÉS en
-- silver.monitoring, avec `sejour_coherent = 0` — la fenêtre ne leur est
-- simplement jamais appliquée, faute de sens. Aucun des 528 ne peut donc
-- atteindre ce troisième contrôle, qui ne s'exerce que sur des séjours
-- COHÉRENTS. La règle reste nécessaire : elle couvre le cas — absent de ce
-- jeu de données, possible dans un futur dépôt — d'un relevé mal horodaté
-- sur un séjour par ailleurs valide.
INSERT INTO
    quarantaine.rejets
SELECT
    'monitoring',
    concat(m.stay_id, '@', toString(m.ts)),
    'releve_hors_sejour',
    'ecarte',
    if(
        m.ts < s.admission_ts,
        concat('ts=', toString(m.ts), ' antérieur à l''admission=', toString(s.admission_ts)),
        concat('ts=', toString(m.ts), ' postérieur à la sortie=', toString(s.discharge_ts))
    ),
    m._jour_depot,
    m._fichier_source,
    '{run_id}',
    now()
FROM
    bronze.monitoring AS m
    INNER JOIN silver.sejours AS s ON m.stay_id = s.stay_id
WHERE
    m.heart_rate BETWEEN 20
    AND 250
    AND m.spo2 BETWEEN 50
    AND 100
    AND m.temp_c BETWEEN 30
    AND 45
    AND (
        m.ts < s.admission_ts
        OR (
            s.discharge_ts IS NOT NULL
            AND m.ts > s.discharge_ts
        )
    );

TRUNCATE TABLE silver.monitoring;

-- Conservé si physiologiquement plausible ET patient identifié (version
-- retenue de bronze.sejours) ; ET, quand le séjour est COHÉRENT (LEFT JOIN
-- silver.sejours), l'horodatage tombe DANS sa fenêtre. Un séjour incohérent
-- n'a pas de fenêtre à vérifier : son relevé est conservé sans condition de
-- date, avec sejour_coherent = 0.
--
-- Le test de cohérence est `sj.stay_id != ''`, PAS `IS NOT NULL` : même
-- piège que dans la section DIAGNOSTICS ci-dessus (`join_use_nulls = 0`, un
-- LEFT JOIN sans correspondance remplit `stay_id` avec la chaîne vide, pas
-- NULL). `IS NULL` sur cette même colonne, plus bas, aurait le même défaut —
-- c'est `= ''` qui est utilisé partout où ce LEFT JOIN est testé.
INSERT INTO
    silver.monitoring
SELECT
    m.stay_id,
    m.ts,
    m.heart_rate,
    m.spo2,
    m.temp_c,
    s.patient_pseudo,
    s.service_code,
    s.admission_ts,
    (sj.stay_id != ''),
    m._jour_depot,
    m._fichier_source,
    '{run_id}',
    now()
FROM
    bronze.monitoring AS m
    INNER JOIN (
        SELECT
            *
        FROM
            (
                SELECT
                    *,
                    row_number() OVER (
                        PARTITION BY stay_id
                        ORDER BY _jour_depot DESC
                    ) AS rang
                FROM
                    bronze.sejours
            )
        WHERE
            rang = 1
    ) AS s ON m.stay_id = s.stay_id
    LEFT JOIN silver.sejours AS sj ON m.stay_id = sj.stay_id
WHERE
    m.heart_rate BETWEEN 20
    AND 250
    AND m.spo2 BETWEEN 50
    AND 100
    AND m.temp_c BETWEEN 30
    AND 45
    AND s.patient_pseudo != ''
    AND (
        sj.stay_id = ''
        OR (
            m.ts >= sj.admission_ts
            AND (
                sj.discharge_ts IS NULL
                OR m.ts <= sj.discharge_ts
            )
        )
    );
