# État des lieux des sources — EDS CHU

Exploration réalisée avant toute décision d'architecture, conformément à la consigne
de la fiche sujet. Tous les chiffres ci-dessous sont reproductibles via les scripts
`exploration/profilage.py` (DuckDB, lecture directe du filestorage en `VARCHAR` pour
constater les formats réels plutôt que les laisser corriger par l'auto-typage).

Périmètre : 28 jours de dépôt — 2026-08-01 à 2026-08-28. Les sources n'ont pas
toutes le même calendrier, et c'est en soi un constat : `sejours`,
`diagnostics` et `monitoring` sont déposés les 28 jours, `patients` seulement
les 26, 27 et 28 août, les référentiels le 1er jour uniquement.

---

## 1. Volumétrie

| Source         | Format        | Lignes brutes                   | Entités distinctes | Couverture                   |
| -------------- | ------------- | ------------------------------- | ------------------ | ---------------------------- |
| `patients`     | CSV           | 18 000 (3 dépôts)               | **6 000 patients** | —                            |
| `sejours`      | CSV           | 6 797                           | 6 797 séjours      | —                            |
| `diagnostics`  | JSON imbriqué | 6 797 objets → **12 720 codes** | 6 797 séjours      | 100 % des séjours            |
| `monitoring`   | Parquet       | **41 778 relevés**              | 872 séjours        | **12,8 % des séjours**       |
| `referentiels` | CSV           | 8 services + 13 codes CIM-10    | —                  | déposé le 1er jour seulement |

---

## 2. Découverte structurante : deux sémantiques d'ingestion différentes

C'est la décision d'architecture n°1, et elle n'est pas annoncée dans la fiche.

### `patients` = snapshot cumulatif

| Jour       | Lignes | Patients apparaissant pour la 1re fois |
| ---------- | ------ | -------------------------------------- |
| 2026-08-26 | 6 000  | 6 000                                  |
| 2026-08-27 | 6 000  | 0                                      |
| 2026-08-28 | 6 000  | 0                                      |

Chaque fichier contient **toute la population connue à date**. Les 6 000 patients
sont présents dans les 3 fichiers. Ingérer naïvement produit 18 000 lignes pour
6 000 patients réels — soit un facteur 3 sur tout indicateur par patient.

**Le calendrier de `patients` n'est pas celui des séjours.** Le snapshot n'est
déposé que sur les trois derniers jours, alors que les séjours couvrent tout le
mois. La population de référence est donc l'**union des dépôts**, pas le dépôt du
jour : un séjour du 1er août est décrit par un fichier patients arrivé 25 jours
plus tard. Tout contrôle de jointure qui comparerait jour à jour prendrait ce
décalage pour une rupture d'intégrité.

### `sejours`, `diagnostics`, `monitoring` = delta pur

Les 6 797 `stay_id` apparaissent **chacun sur un seul jour**, celui de l'admission
(vérifié : les admissions du fichier du 1er août sont toutes datées du 1er août).

**Conséquence** : le pipeline ne peut pas appliquer la même stratégie aux quatre
sources. `patients` exige une déduplication / upsert ; les trois autres un simple
append. Une ingestion uniforme est fausse dans un sens ou dans l'autre.

---

## 3. Contrôles qualité — résultats chiffrés

### 3.1 Contrôles demandés par la fiche

| Source       | Contrôle attendu             | Résultat                                                    | Traitement               |
| ------------ | ---------------------------- | ----------------------------------------------------------- | ------------------------ |
| `patients`   | Doublons (redépôt quotidien) | **12 000 lignes en trop** (18 000 → 6 000)                  | Dédupliquer              |
| `patients`   | Valeurs manquantes / formats | **0** sur les 7 colonnes                                    | —                        |
| `patients`   | Sexe normalisé M/F           | **0 anomalie** (9 045 M / 8 955 F)                          | —                        |
| `patients`   | Dates valides                | **0 anomalie** — 100 % ISO, bornes 1931-01-04 → 2025-12-23  | —                        |
| `sejours`    | Cohérence temporelle         | **68** séjours avec `discharge_ts < admission_ts`           | Écarter                  |
| `sejours`    | Séjour en cours              | **683** `discharge_ts` vides                                | **Conserver** (légitime) |
| `monitoring` | Hors plage physiologique     | **858** relevés                                             | Écarter                  |

**Point notable sur la déduplication `patients` :** **118 patients sur 6 000** ont
au moins un attribut qui diverge entre leurs redépôts. La règle « garder la version
la plus récente » n'est donc pas une précaution théorique : sans elle, la version
retenue dépendrait de l'ordre de lecture des fichiers, et deux exécutions du
pipeline pourraient produire deux entrepôts différents. C'est ce que fixe l'`argMax`
sur le jour de dépôt — un choix déterministe et justifiable, pas un hasard.

### 3.2 Intégrité référentielle — totalement propre

`service_code` hors référentiel : **0** · `patient_id` orphelin : **0** ·
`stay_id` orphelin (diagnostics) : **0** · `stay_id` orphelin (monitoring) : **0** ·
code CIM-10 hors référentiel : **0** · séjour sans diagnostic : **0** ·
séjours avec exactement un diagnostic principal : **6 797 / 6 797**.

Aucun nettoyage référentiel n'est nécessaire. À dire explicitement : c'est une
vérification menée, pas une vérification omise.

### 3.3 Anomalies **non prévues** par la fiche

Ce sont celles qui font la différence — la fiche invite explicitement à « en repérer
d'autres en explorant ».

**a) `discharge_mode` absent : uniquement sur les séjours en cours — contrôle mené, anomalie absente**

|                 | `discharge_mode` présent | absent        |
| --------------- | ------------------------ | ------------- |
| Séjour clos     | 6 114                    | **0**         |
| Séjour en cours | —                        | 683 (attendu) |

Le mode de sortie n'est vide que là où il doit l'être : sur les séjours sans date de
sortie. **Aucun séjour clos n'est concerné.** Le cas est donc à traiter comme un
troisième état possible — ni séjour en cours, ni incohérence temporelle — mais ce
dépôt ne l'exerce pas. La règle retenue reste la même : conserver le séjour avec un
mode `inconnu` explicite plutôt que l'écarter, de sorte que la DMS demeure calculable
si le cas se présente. À écrire tel quel dans le dossier : c'est une vérification
menée, pas une vérification omise.

**b) Les valeurs aberrantes du monitoring sont un marqueur de panne capteur — 858 relevés**

FC et SpO2 sont **toujours aberrantes ensemble** (858 fois les deux, 0 fois l'une
seule), sur exactement 4 combinaisons de butée :

| `heart_rate` | `spo2` | n   |
| ------------ | ------ | --- |
| 500          | 120    | 239 |
| 500          | 0      | 223 |
| 0            | 0      | 200 |
| 0            | 120    | 196 |

Ce n'est pas du bruit de mesure, c'est un capteur déconnecté émettant ses valeurs
de saturation. La température reste valide sur ces lignes (**0 hors plage**).
Décision de conception à documenter : écarter le relevé entier, ou n'invalider que
FC/SpO2 en conservant `temp_c` ? Le choix change le dénominateur du KPI d'alerte.

**c) Relevés postérieurs à la sortie du patient — 528 relevés sur 11 séjours**

528 relevés après `discharge_ts`, **0 avant l'admission**. Concentrés sur 11 séjours
seulement : c'est un défaut d'arrêt de capteur, pas un bruit diffus.

---

## 4. Faisabilité des indicateurs demandés

### 4.1 Le monitoring ne couvre que 2 services sur 8

| Service                                    | Séjours | Monitorés | %          |
| ------------------------------------------ | ------- | --------- | ---------- |
| CARDIO                                     | 1 615   | 677       | **41,9 %** |
| REA                                        | 473     | 195       | **41,2 %** |
| URGENCES, ONCO, PNEUMO, CHIR, PEDIA, NEURO | 4 709   | **0**     | **0 %**    |

Un dashboard « relevés en alerte par service » affichera 6 lignes à zéro et sera lu
comme un bug. Le périmètre doit être restreint à REA + CARDIO et annoncé sur la vue.

### 4.2 Le monitoring déborde de son jour de dépôt

Le fichier du 1er août contient des relevés jusqu'au **3 août à 22:57**, et le
décalage se reproduit sur les 28 dépôts : chaque fichier couvre son jour et les deux
suivants. Le jour de dépôt n'est donc pas la date de la donnée. Le KPI « relevés en
alerte / jour » doit être agrégé sur `ts`, jamais sur le jour de dépôt — sinon les
alertes du 3 août sont attribuées au 1er.

Volumétrie d'alerte sur les 40 920 relevés valides (seuils indicatifs à valider) :
FC > 120 ou < 40 → 277 · SpO2 < 92 → 1 127 · temp > 38,5 °C → 1 082.

### 4.3 Le taux de réadmission à 30 jours reste tronqué, mais de peu

891 paires de réadmission ≤ 30 jours, avec des délais de **0 à 23 jours** : les
admissions couvrent 28 jours, donc la fenêtre d'observation reste plus courte que la
fenêtre de l'indicateur, mais l'écart n'est plus que de deux jours. Le taux calculé
est encore un plancher — un patient sorti le 28 août ne peut pas être observé
jusqu'au 27 septembre — et il faut le dire, mais il devient exploitable comme ordre
de grandeur. **À énoncer en limite du dossier.**

**Et 152 de ces paires suivent un séjour dont `discharge_mode = 'deces'`** : un
patient déclaré décédé puis réadmis. Soit une incohérence de données, soit un mode de
sortie mal renseigné. Dans les deux cas, les séjours index avec sortie `deces` doivent
être exclus du dénominateur — c'est la règle métier standard et elle se justifie.

Répartition des séjours par patient : 5 229 patients avec 1 séjour, 745 avec 2, et 26
avec 3 sur le mois.

---

## 5. RGPD — le risque de ré-identification est quantifié

La fiche demande de généraliser `birth_date` en **année**. Mesure du k-anonymat sur
les quasi-identifiants qui subsisteraient dans l'entrepôt :

**Avec l'année de naissance exacte + sexe + région :**

| Classe              | Groupes | Patients | % population |
| ------------------- | ------- | -------- | ------------ |
| k ≥ 5 (conforme)    | 538     | 3 740    | 62,3 %       |
| k = 2..4            | 713     | 2 075    | 34,6 %       |
| **k = 1 (uniques)** | 185     | **185**  | **3,1 %**    |

**37,7 % de la population reste sous le seuil de 5**, et 185 patients sont
directement isolables par le triplet (année, sexe, région).

**Avec une tranche d'âge de 10 ans + sexe + région :**

| Classe | Groupes | Patients  | % population |
| ------ | ------- | --------- | ------------ |
| k ≥ 5  | 160     | **6 000** | **100 %**    |

La généralisation en tranches de 10 ans amène **100 % de la population à k ≥ 5**, et
fait tomber de **95 à 13** le nombre de cohortes recherche sous le seuil de 5
patients.

**Elle ne suffit cependant pas à elle seule.** Treize cohortes restent sous le
seuil après généralisation, parce que la nomenclature comporte trois pathologies
rares — mucoviscidose, amyotrophie spinale, trisomie 21 — dont les effectifs sont
faibles quelle que soit la granularité de l'âge. La généralisation réduit le risque,
elle ne l'annule pas : un **filtre explicite `>= 5 patients` à l'écriture** reste
indispensable, et c'est lui qui garantit la propriété.

**Recommandation** : conserver l'année de naissance dans la couche pilotage (accès
restreint, justifié par le besoin), n'exposer que la tranche de 10 ans dans la couche
recherche, **et** filtrer les petits effectifs à l'écriture. C'est un argument
chiffré, pas une précaution de principe — et il répond directement au critère
« petits effectifs » de la fiche.

À noter également : `patient_id` est un IPP en clair. C'est à la fois la clé de
jointure et un identifiant : il doit être pseudonymisé par hachage déterministe salé,
et non conservé tel quel.

---

## 6. Ce que cet état des lieux implique pour l'architecture

1. **Deux stratégies d'ingestion distinctes** — upsert pour `patients`, append pour les trois autres.
2. **Partitionner sur la donnée, pas sur le dépôt** — l'écart entre jour de dépôt et `ts` du monitoring impose de dater les faits par leur horodatage métier.
3. **Une couche de rejet est indispensable** — 68 + 858 lignes à écarter (926 au
   total), à isoler et à compter, jamais à supprimer silencieusement — et un
   mécanisme de **signalement**, distinct du rejet, pour les 528 relevés
   postérieurs à la sortie : 520 d'entre eux portent un séjour dont la
   cohérence temporelle est elle-même en cause (§ 3.1) et échappent de ce fait
   au contrôle plutôt que de l'enfreindre, seuls 8 étant réellement écartés
   pour capteur hors plage (§ 3.3.b). Décision prise après l'exploration :
   documentée au rapport de conception, §§ 2.8-2.9.
4. **Trois arbitrages à documenter** : le sort de `discharge_mode` absent, le sort de `temp_c` sur les relevés à capteur en panne, le sort des relevés post-sortie.
5. **Le cloisonnement pilotage / recherche a une traduction technique précise** : granularité de l'âge différente entre les deux couches, et non seulement des droits d'accès différents.

---

## 7. Reproduire ces chiffres

```
python3 -m venv .venv && .venv/bin/pip install duckdb
.venv/bin/python -m exploration.profilage                  # les six sections

# ou une section à la fois
.venv/bin/python -m exploration.profilage inventaire       # volumétrie, schémas, référentiels
.venv/bin/python -m exploration.profilage patients         # qualité patients
.venv/bin/python -m exploration.profilage sejours          # qualité séjours, nature des dépôts
.venv/bin/python -m exploration.profilage diagnostics      # structure, intégrité référentielle
.venv/bin/python -m exploration.profilage monitoring       # aberrations, couverture, alertes
.venv/bin/python -m exploration.profilage reidentification # k-anonymat
```
