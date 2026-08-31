# État des lieux des sources — EDS CHU

Exploration réalisée avant toute décision d'architecture, conformément à la consigne
de la fiche sujet. Tous les chiffres ci-dessous sont reproductibles via les scripts
`exploration/0*.py` (DuckDB, lecture directe du filestorage en `VARCHAR` pour
constater les formats réels plutôt que les laisser corriger par l'auto-typage).

Périmètre : 3 jours de dépôt — 2026-08-26, 27 et 28.

---

## 1. Volumétrie

| Source         | Format        | Lignes brutes                    | Entités distinctes | Couverture                   |
| -------------- | ------------- | -------------------------------- | ------------------ | ---------------------------- |
| `patients`     | CSV           | 16 200                           | **6 000 patients** | —                            |
| `sejours`      | CSV           | 15 000                           | 15 000 séjours     | —                            |
| `diagnostics`  | JSON imbriqué | 15 000 objets → **37 380 codes** | 15 000 séjours     | 100 % des séjours            |
| `monitoring`   | Parquet       | **66 677 relevés**               | 1 506 séjours      | **10 % des séjours**         |
| `referentiels` | CSV           | 8 services + 10 codes CIM-10     | —                  | déposé le 1er jour seulement |

---

## 2. Découverte structurante : deux sémantiques d'ingestion différentes

C'est la décision d'architecture n°1, et elle n'est pas annoncée dans la fiche.

### `patients` = snapshot cumulatif

| Jour       | Lignes | Patients apparaissant pour la 1re fois |
| ---------- | ------ | -------------------------------------- |
| 2026-08-26 | 4 800  | 4 800                                  |
| 2026-08-27 | 5 400  | 600                                    |
| 2026-08-28 | 6 000  | 600                                    |

Chaque fichier contient **toute la population connue à date**. 4 800 patients sont
présents dans les 3 fichiers. Ingérer naïvement produit 16 200 lignes pour
6 000 patients réels — soit un facteur 2,7 sur tout indicateur par patient.

### `sejours`, `diagnostics`, `monitoring` = delta pur

Les 15 000 `stay_id` apparaissent **chacun sur un seul jour**, celui de l'admission
(vérifié : les admissions du fichier du 26 sont toutes datées du 26, de 00:01 à 23:59).

**Conséquence** : le pipeline ne peut pas appliquer la même stratégie aux quatre
sources. `patients` exige une déduplication / upsert ; les trois autres un simple
append. Une ingestion uniforme est fausse dans un sens ou dans l'autre.

---

## 3. Contrôles qualité — résultats chiffrés

### 3.1 Contrôles demandés par la fiche

| Source       | Contrôle attendu             | Résultat                                                                   | Traitement               |
| ------------ | ---------------------------- | -------------------------------------------------------------------------- | ------------------------ |
| `patients`   | Doublons (redépôt quotidien) | **10 200 lignes en trop** (16 200 → 6 000)                                 | Dédupliquer              |
| `patients`   | Valeurs manquantes / formats | **0** sur les 7 colonnes                                                   | —                        |
| `patients`   | Sexe normalisé M/F           | **0 anomalie** (8 132 M / 8 068 F)                                         | —                        |
| `patients`   | Dates valides                | **0 anomalie** — 100 % ISO, bornes 1930-01-02 → 2020-12-28                 | —                        |
| `sejours`    | Cohérence temporelle         | **136** séjours avec `discharge_ts < admission_ts` (44 / 50 / 42 par jour) | Écarter                  |
| `sejours`    | Séjour en cours              | **1 190** `discharge_ts` vides                                             | **Conserver** (légitime) |
| `monitoring` | Hors plage physiologique     | **1 369** relevés                                                          | Écarter                  |

**Point notable sur la déduplication `patients` :** aucun patient n'a d'attribut
divergent entre ses redépôts (0 sur 6 000, sur les 6 colonnes). La règle « garder la
version la plus récente » est donc sans effet pratique sur ce jeu — mais la
déduplication reste obligatoire. À écrire tel quel dans le dossier : c'est une règle
correcte dont on a vérifié qu'elle n'introduisait aucune perte ici.

### 3.2 Intégrité référentielle — totalement propre

`service_code` hors référentiel : **0** · `patient_id` orphelin : **0** ·
`stay_id` orphelin (diagnostics) : **0** · `stay_id` orphelin (monitoring) : **0** ·
code CIM-10 hors référentiel : **0** · séjour sans diagnostic : **0** ·
séjours avec exactement un diagnostic principal : **15 000 / 15 000**.

Aucun nettoyage référentiel n'est nécessaire. À dire explicitement : c'est une
vérification menée, pas une vérification omise.

### 3.3 Anomalies **non prévues** par la fiche

Ce sont celles qui font la différence — la fiche invite explicitement à « en repérer
d'autres en explorant ».

**a) `discharge_mode` absent sur un séjour pourtant clos — 1 992 lignes**

|                 | `discharge_mode` présent | absent          |
| --------------- | ------------------------ | --------------- |
| Séjour clos     | 11 818                   | **1 992**       |
| Séjour en cours | —                        | 1 190 (attendu) |

1 992 séjours ont une date de sortie mais pas de mode de sortie. Ce n'est ni un
séjour en cours ni une incohérence temporelle : c'est un troisième cas. Il impacte
toute analyse par mode de sortie (dont la mortalité). À arbitrer : conserver le
séjour avec un mode `inconnu` explicite plutôt que l'écarter — la DMS reste calculable.

**b) Les valeurs aberrantes du monitoring sont un marqueur de panne capteur — 1 369 relevés**

FC et SpO2 sont **toujours aberrantes ensemble** (1 369 fois les deux, 0 fois l'une
seule), sur exactement 4 combinaisons de butée :

| `heart_rate` | `spo2` | n   |
| ------------ | ------ | --- |
| 500          | 120    | 354 |
| 0            | 120    | 345 |
| 0            | 0      | 339 |
| 500          | 0      | 331 |

Ce n'est pas du bruit de mesure, c'est un capteur déconnecté émettant ses valeurs
de saturation. La température reste valide sur ces lignes (globalement 36,4 – 40,0 °C,
**0 hors plage**). Décision de conception à documenter : écarter le relevé entier, ou
n'invalider que FC/SpO2 en conservant `temp_c` ? Le choix change le dénominateur du
KPI d'alerte.

**c) Relevés postérieurs à la sortie du patient — 520 relevés sur 12 séjours**

520 relevés après `discharge_ts`, **0 avant l'admission**. Concentrés sur 12 séjours
seulement : c'est un défaut d'arrêt de capteur, pas un bruit diffus.

---

## 4. Faisabilité des indicateurs demandés

### 4.1 Le monitoring ne couvre que 2 services sur 8

| Service                                    | Séjours | Monitorés | %          |
| ------------------------------------------ | ------- | --------- | ---------- |
| REA                                        | 1 914   | 778       | **40,6 %** |
| CARDIO                                     | 1 844   | 728       | **39,5 %** |
| URGENCES, ONCO, PNEUMO, CHIR, PEDIA, NEURO | 11 242  | **0**     | **0 %**    |

Un dashboard « relevés en alerte par service » affichera 6 lignes à zéro et sera lu
comme un bug. Le périmètre doit être restreint à REA + CARDIO et annoncé sur la vue.

### 4.2 Le monitoring déborde de son jour de dépôt

Le fichier du 26 contient des relevés jusqu'au **28 à 22:58**. Le jour de dépôt n'est
donc pas la date de la donnée. Le KPI « relevés en alerte / jour » doit être agrégé
sur `ts`, jamais sur le jour de dépôt — sinon les alertes du 28 sont attribuées au 26.

Volumétrie d'alerte sur les 65 308 relevés valides (seuils indicatifs à valider) :
FC > 120 ou < 40 → 460 · SpO2 < 92 → 1 691 · temp > 38,5 °C → 1 748.

### 4.3 Le taux de réadmission à 30 jours est structurellement tronqué

1 685 paires de réadmission ≤ 30 jours, mais avec des délais de **0 à 3 jours
seulement** : les admissions ne couvrent que 3 jours, donc la fenêtre d'observation
est plus courte que la fenêtre de l'indicateur. Le taux calculé est un plancher, pas
le taux réel. **À énoncer en limite du dossier** — c'est exactement le type de réserve
attendu.

**Et 223 de ces paires suivent un séjour dont `discharge_mode = 'deces'`** : un
patient déclaré décédé puis réadmis. Soit une incohérence de données, soit un mode de
sortie mal renseigné. Dans les deux cas, les séjours index avec sortie `deces` doivent
être exclus du dénominateur — c'est la règle métier standard et elle se justifie.

Répartition des séjours par patient : 1 216 patients avec 1 séjour, jusqu'à 4 patients
avec 10 séjours sur 3 jours.

---

## 5. RGPD — le risque de ré-identification est quantifié

La fiche demande de généraliser `birth_date` en **année**. Mesure du k-anonymat sur
les quasi-identifiants qui subsisteraient dans l'entrepôt :

**Avec l'année de naissance exacte + sexe + région :**

| Classe              | Groupes | Patients | % population |
| ------------------- | ------- | -------- | ------------ |
| k ≥ 5 (conforme)    | 572     | 3 497    | 58,3 %       |
| k = 2..4            | 762     | 2 401    | 40,0 %       |
| **k = 1 (uniques)** | 102     | **102**  | **1,7 %**    |

**41,7 % de la population reste sous le seuil de 5**, et 102 patients sont
directement isolables par le triplet (année, sexe, région).

**Avec une tranche d'âge de 10 ans + sexe + région :**

| Classe | Groupes | Patients  | % population |
| ------ | ------- | --------- | ------------ |
| k ≥ 5  | 160     | **6 000** | **100 %**    |

La généralisation en tranches de 10 ans amène **100 % de la population à k ≥ 5**, et
fait tomber à **0** le nombre de cohortes recherche sous le seuil de 5 patients
(contre 284 avec l'année exacte).

**Recommandation** : conserver l'année de naissance dans la couche pilotage (accès
restreint, justifié par le besoin), mais n'exposer que la tranche de 10 ans dans la
couche recherche. C'est un argument chiffré, pas une précaution de principe — et il
répond directement au critère « petits effectifs » de la fiche.

À noter également : `patient_id` est un IPP en clair. C'est à la fois la clé de
jointure et un identifiant : il doit être pseudonymisé par hachage déterministe salé,
et non conservé tel quel.

---

## 6. Ce que cet état des lieux implique pour l'architecture

1. **Deux stratégies d'ingestion distinctes** — upsert pour `patients`, append pour les trois autres.
2. **Partitionner sur la donnée, pas sur le dépôt** — l'écart entre jour de dépôt et `ts` du monitoring impose de dater les faits par leur horodatage métier.
3. **Une couche de rejet est indispensable** — 136 + 1 369 + 520 lignes à écarter, à isoler et à compter, jamais à supprimer silencieusement.
4. **Trois arbitrages à documenter** : le sort de `discharge_mode` absent, le sort de `temp_c` sur les relevés à capteur en panne, le sort des relevés post-sortie.
5. **Le cloisonnement pilotage / recherche a une traduction technique précise** : granularité de l'âge différente entre les deux couches, et non seulement des droits d'accès différents.

---

## 7. Reproduire ces chiffres

```
python3 -m venv .venv && .venv/bin/pip install duckdb
.venv/bin/python exploration/00_inventaire.py        # volumétrie, schémas, référentiels
.venv/bin/python exploration/01_patients.py          # qualité patients
.venv/bin/python exploration/02_sejours.py           # qualité séjours, nature des dépôts
.venv/bin/python exploration/03_diag_monitoring.py   # diagnostics, monitoring, intégrité croisée
.venv/bin/python exploration/04_affinage.py          # nature des aberrations, couverture
.venv/bin/python exploration/05_reidentification.py  # k-anonymat
```
