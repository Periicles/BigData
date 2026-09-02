# Entrepôt de Données de Santé — CHU

Pipeline complet d'un EDS : collecte quotidienne des dépôts du CHU,
pseudonymisation à l'ingestion, transformation en couches, jusqu'à deux bases
**gold** cloisonnées — modèle en étoile pour le pilotage, agrégats anonymisés
pour la recherche.

**Tout s'exécute en local, en deux commandes.** Aucune étape manuelle : la
construction de l'entrepôt et l'attribution des droits sont entièrement
scriptées.

---

## Ce que fait le pipeline

```
source-filestorage/     dépôt du CHU, lecture seule (identités en clair)
        │
        │  pseudonymisation en flux — les identités ne touchent jamais le disque
        ▼
   lake/                copie pseudonymisée
        ▼
   bronze               tables typées, partitionnées par jour de dépôt
        ▼
   silver               nettoyé, dédupliqué, enrichi
        │  └──────────►  quarantaine     chaque ligne écartée, avec son motif
        ▼
   gold_pilotage                          gold_recherche
   indicateurs agrégés                    agrégats · k ≥ 5
   kpi_dms_service · kpi_urgences_jour    coh_prevalence
   kpi_readmission_service                coh_description
   kpi_alertes_jour
     ╰─ dérivés du modèle en étoile
        fact_sejour · fact_diagnostic · fact_releve
        dim_patient · dim_service · dim_cim10
        │                                        │
        └──── deux bases, deux comptes, droits disjoints ────┘
             le refus est prononcé par le moteur, pas par l'applicatif
```

**Où passe la frontière entre silver et gold.** Une règle, appliquée partout :

| | |
|---|---|
| Règle de **validité** de la donnée, fournie par le sujet | **silver** — plages physiologiques, cohérence temporelle, déduplication |
| Règle **métier**, que le sujet ne fournit pas et qui se paramètre | **gold** — seuils d'alerte, âge à l'événement |

C'est pour cela que `silver.monitoring` ne porte aucun drapeau d'alerte (il
n'existe aucun seuil réglementaire : ce sont des valeurs par défaut de
constructeur que chaque service ajuste — voir `eds/config.py`), et que
`silver.sejours` ne porte pas l'âge : celui-ci croise `dim_patient.birth_year`
et la date d'admission du fait, il se calcule donc contre la dimension, à la
construction de l'étoile.

Les seuils se changent sans toucher au SQL :

```bash
EDS_SEUIL_FC_BASSE=45 .venv/bin/python -m eds.run --tout
```

---

## Prérequis

| | |
|---|---|
| Docker Desktop | démarré (`docker info` doit répondre) |
| Python | 3.11 ou plus |
| Ports libres | `8123` ClickHouse |
| Données source | `eds-chu-sujet/source-filestorage/` — voir ci-dessous |

> **Les données ne sont pas dans ce dépôt, volontairement.** Les fichiers
> `patients.csv` contiennent des identités en clair (nom, prénom, NIR). Les
> versionner ferait entrer des données de santé nominatives dans l'historique
> Git, où elles resteraient — un commit ne se retire pas. Le répertoire
> `eds-chu-sujet/source-filestorage/` est donc exclu par `.gitignore` : placez-y
> le dépôt fourni par le CHU avant de lancer le pipeline.

---

## Démarrage

```bash
# 1. Secrets — génère le sel de 256 bits et les quatre mots de passe
python3 - <<'EOF'
import secrets, pathlib
mdp = lambda n=24: secrets.token_urlsafe(n)
pathlib.Path(".env").write_text(f"""# ── ClickHouse ──
CH_ADMIN_USER=eds_admin
CH_ADMIN_PASSWORD={mdp()}
CH_PILOTAGE_PASSWORD={mdp()}
CH_RECHERCHE_PASSWORD={mdp()}
CH_EXPLOITATION_PASSWORD={mdp()}

# ── Pseudonymisation ──
EDS_PSEUDO_SALT={secrets.token_hex(32)}
""")
EOF

# 2. Environnement Python
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 3. Entrepôt
docker compose up -d

# 4. Pipeline complet
.venv/bin/python -m eds.run
```

Comptez **environ 1,5 seconde** pour l'intégralité du pipeline.

> **La couche de restitution est retirée en attendant l'arbitrage de
> l'intervenant.** Le pipeline s'arrête donc à `gold`, qui est l'objet du
> dossier : deux bases, un modèle en étoile, des agrégats anonymisés, et un
> cloisonnement prononcé par le moteur. Les tableaux de bord se rebranchent sur
> ces mêmes tables sans rien changer en amont.

### Accès

| | |
|---|---|
| Console SQL | http://localhost:8123/play — utilisateur `eds_admin` |

**Quatre comptes ClickHouse, un par usage.** Le sujet définit deux publics
métier — « pilotage et recherche ne voient pas les mêmes données → droits
d'accès distincts ». S'y ajoute l'administration de l'entrepôt, rôle à part
entière dans un EDS : c'est elle qui exploite le pipeline, accorde les
habilitations et répond d'une demande d'effacement. Chacun de ces usages a son
compte, et aucun n'a plus de droits que son besoin.

| Compte ClickHouse | Usage | Droits |
|---|---|---|
| `eds_admin` | le pipeline | tous — il crée les tables et applique les habilitations |
| `eds_pilotage` | **Direction hospitalière** — piloter l'activité et la qualité des soins | `SELECT` sur les **4 tables d'indicateurs**, plus **16 colonnes** des faits — ni `patient_pseudo`, ni `stay_id` |
| `eds_recherche` | **Recherche clinique** — décrire des cohortes | `SELECT` sur les colonnes des deux tables de cohortes |
| `eds_exploitation` | **Investigation technique** — incident, piste d'audit, effacement | `SELECT` sur `bronze`, `silver`, `quarantaine`, `ops` — **lecture seule** |

Mots de passe dans `.env` — `CH_*_PASSWORD`. Aucun n'est écrit en dur dans le
code.

> **Les droits sont posés colonne par colonne, pas base par base.** Un `GRANT`
> sur `gold_pilotage` entier donnerait accès à `patient_pseudo` et au grain du
> séjour — très au-delà du besoin d'une direction, qui n'a jamais à désigner un
> patient. Le compte de pilotage ne peut donc ni lire le pseudonyme, ni
> dénombrer des patients, ni faire un `SELECT *`, ni atteindre `dim_patient` et
> `fact_diagnostic` : le moteur refuse. Ses indicateurs, eux, fonctionnent.
>
> **Cette borne ne dépend pas de qui interroge, mais du compte employé.**
> Quiconque se connecte avec `eds_pilotage` — administrateur compris — se voit
> opposer le même refus. C'est ce qui distingue un cloisonnement d'un réglage
> d'interface : aucun outil placé au-dessus ne peut le contourner, puisque le
> refus vient du moteur.

> Le compte d'investigation n'est **pas** celui du pipeline. `eds_admin` peut
> créer et supprimer des bases : ce pouvoir n'a pas sa place dans un usage
> quotidien. `eds_exploitation` ne peut rien écrire, quelle que soit la requête
> saisie — le moteur refuse.

---

## Lancer et rejouer

```bash
.venv/bin/python -m eds.run                      # incrémental : les jours non ingérés
.venv/bin/python -m eds.run --jour 2026-08-27    # rejoue un jour précis
.venv/bin/python -m eds.run --tout               # recharge tout le dépôt
.venv/bin/python -m eds.run --etat               # état de l'entrepôt, sans rien modifier
```

**Le pipeline est idempotent.** Rejouer un jour réécrit sa partition : les
compteurs ne bougent pas et aucun doublon n'apparaît. Rejouer un dépôt déjà
traité ne fait que reconstruire silver et gold.

**Le mode par défaut est incrémental.** Seuls les jours absents de l'entrepôt
sont ingérés ; les anciens ne sont ni relus ni dupliqués.

### Planification

```bash
crontab ops/crontab.example     # exécution quotidienne à 03h10
```

---

## Vérifier

Huit contrôles, exécutables à tout moment. Ils constituent la démonstration
des propriétés annoncées.

```bash
.venv/bin/python -m tests.verifier      # les quatre contrôles d'un coup
.venv/bin/python -m tests.demontrer     # les quatre démonstrations

# ou une section à la fois
.venv/bin/python -m tests.verifier pseudonymisation   # aucune identité dans le lake
.venv/bin/python -m tests.verifier qualite            # bronze = silver + quarantaine
.venv/bin/python -m tests.verifier indicateurs        # les 6 indicateurs du sujet
.venv/bin/python -m tests.verifier rgpd               # les 5 contraintes du sujet
.venv/bin/python -m tests.demontrer cloisonnement     # droits d'accès disjoints
.venv/bin/python -m tests.demontrer reprise           # erreurs et reprise sur incident
.venv/bin/python -m tests.demontrer qualite           # les contrôles face à des lignes fautives
.venv/bin/python -m tests.demontrer effectifs         # le seuil des 5 patients, de part et d'autre
```

| Contrôle | Ce qu'il prouve |
|---|---|
| `verifier pseudonymisation` | Les 17 384 valeurs identifiantes de la source sont introuvables dans le lake ; aucune collision de pseudonyme ; les jointures survivent |
| `verifier qualite` | Équation de conservation par source, déduplication, règles métier du §3, intégrité référentielle de silver **et** du modèle en étoile — 32 contrôles |
| `verifier indicateurs` | Les six indicateurs du §4, calculés depuis gold : leur **valeur restituée** et la propriété qui la fonde — dénominateur de la DMS, inclusion numérateur/dénominateur de la réadmission, seuils d'alerte effectivement issus de la configuration, fidélité des agrégats de recherche — 18 contrôles |
| `verifier rgpd` | Les cinq contraintes RGPD, vérifiées sur l'entrepôt réel : pseudonymisation, minimisation, cloisonnement, petits effectifs, traçabilité — plus l'absence de donnée personnelle dans les journaux |
| `demontrer cloisonnement` | Chaque compte accède à sa base et se voit refuser les trois autres, par le moteur |
| `demontrer reprise` | Erreurs détectées, tracées, entrepôt cohérent, reprise par simple relance |
| `demontrer qualite` | Des lignes fautives sont injectées en bronze : dates illisibles écartées, sexe hors nomenclature corrigé, casse redressée sans bruit, équation de conservation intacte — puis l'entrepôt est remis en état |
| `demontrer effectifs` | Deux cohortes sont fabriquées de part et d'autre du seuil RGPD : celle de 4 patients existe au grain du fait mais n'atteint pas la base recherche, celle de 5 passe — le filtre coupe **sous** 5, pas à 5 |

> **Pourquoi fabriquer des cas qui n'existent pas.** Les données fournies sont
> propres sur deux des garanties annoncées : dates valides et sexe normalisé
> M/F. Une règle qu'aucune donnée n'exerce ne prouve rien : `demontrer qualite`
> fabrique donc ces cas, vérifie ce que l'entrepôt en fait, puis recharge bronze
> depuis le lake.
>
> Le seuil des petits effectifs, lui, **se déclenche sur les données réelles** :
> deux prévalences (trisomie 21, 3 patients ; mucoviscidose, 4) et treize
> cohortes de description sont supprimées à la construction. `demontrer
> effectifs` reste utile pour une autre raison — il fabrique le cas limite exact,
> une cohorte de 4 et une de 5, et montre que la coupe tombe **sous** 5 et non à
> 5.

---

## Reprise sur incident

Le journal se lit à deux endroits : `logs/pipeline.log` (une ligne JSON par
événement) et la table `ops.executions` (bilan par étape, interrogeable en SQL).

```sql
-- Les dernières exécutions, succès comme échecs
SELECT demarre_a, run_id, etape, statut, lignes, duree_s, message
FROM ops.executions ORDER BY demarre_a DESC LIMIT 20;
```

**Principe de reprise : il n'y a rien à restaurer.** Bronze est la source de
vérité durable ; silver et gold en sont intégralement reconstructibles. Après
un incident, on corrige la cause et on relance — l'exécution est idempotente.

| Symptôme | Cause | Correction |
|---|---|---|
| `ClickHouse ne voit pas le lake` | Le répertoire `lake/` a été supprimé : le montage Docker pointe sur un inode disparu | `docker compose restart clickhouse` |
| `Connection reset by peer` | ClickHouse redémarre | Aucune — le pipeline retente avec temporisation exponentielle |
| `Variable d'environnement manquante` | `.env` absent ou incomplet | Reprendre l'étape 1 du démarrage |
| `argument invalide` | Jour mal formé en ligne de commande | Utiliser le format `AAAA-MM-JJ` |
| `aucun fichier trouvé pour le …` | Jour absent du dépôt du CHU | Vérifier `eds-chu-sujet/source-filestorage/` |
| `Unknown expression identifier` sur une colonne | Un DDL a été modifié : `CREATE TABLE IF NOT EXISTS` **ne migre pas** un schéma existant | Supprimer la table concernée (`DROP TABLE …`) puis relancer le pipeline |

**Repartir de zéro** (destructif, l'entrepôt est reconstruit intégralement) :

```bash
docker compose down -v && docker compose up -d
.venv/bin/python -m eds.run --tout
```

---

## Documentation

| Document | Contenu |
|---|---|
| [`docs/RAPPORT.md`](docs/RAPPORT.md) | **Le rapport de conception** — besoin métier, choix et justifications, architecture, limites et recommandations. Le document à lire en premier. |
| [`exploration/RAPPORT-EXPLORATION.md`](exploration/RAPPORT-EXPLORATION.md) | L'état des lieux des sources, établi **avant** toute décision d'architecture : volumétrie, anomalies chiffrées, mesure du risque de ré-identification. |
| [`sql/99_verifications.sql`](sql/99_verifications.sql) | Requêtes d'inspection à exécuter dans la console SQL, commentées. |

## Conformité RGPD

Les cinq contraintes du sujet sont vérifiables en une commande
(`python -m tests.verifier rgpd`) :

| Contrainte | Mise en œuvre | Où |
|---|---|---|
| **Pseudonymisation** | HMAC-SHA256 salé appliqué **pendant la copie** : les identités ne sont écrites nulle part. Aucune colonne `nir`, `nom`, `prenom`, `birth_date` ni `patient_id` n'existe dans l'entrepôt. | `eds/lake.py` |
| **Minimisation** | Trois colonnes supprimées à la source, date de naissance généralisée à l'année. La base recherche n'expose ni `birth_year`, ni `patient_pseudo`, ni `region`. | `eds/lake.py`, `sql/31_gold_transform.sql` |
| **Cloisonnement** | Deux bases séparées, quatre comptes bornés, droits posés **colonne par colonne** sur la couche gold. Le refus vient du moteur. | `sql/50_droits.sql` |
| **Petits effectifs** | `HAVING count(DISTINCT patient) >= 5` appliqué **à l'écriture** : aucune cohorte sous seuil n'existe dans la base. | `sql/31_gold_transform.sql` |
| **Traçabilité** | Chaque ligne porte son fichier d'origine, son horodatage d'ingestion et l'identifiant du run. Le journal `ops.executions` conserve chaque étape, succès comme échec. | `sql/10_bronze.sql`, `sql/60_ops.sql` |

> **Les journaux ne contiennent aucune donnée personnelle** — ni pseudonyme, ni
> identifiant patient. C'est vérifié automatiquement, sur le fichier de log comme
> sur la table `ops.executions`.
>
> **Le sel de pseudonymisation n'est pas versionné.** Sa perte rend tout
> rapprochement avec la source définitivement impossible — c'est la propriété
> recherchée, et elle distingue une pseudonymisation d'un simple encodage.

## Organisation du dépôt

```
docker-compose.yml       ClickHouse 25.8, version épinglée
requirements.txt         2 dépendances Python

eds/                     le pipeline
  config.py              chemins et secrets, sans dépendance externe
  lake.py                copie transformante en flux + pseudonymisation
  warehouse.py           client ClickHouse, exécution SQL, chargement bronze
  journal.py             journalisation JSON + console
  run.py                 orchestrateur — point d'entrée

sql/                     toute la transformation, versionnée
  00_databases.sql       les six bases
  10_bronze.sql          tables typées, partitionnées
  15_quarantaine.sql     registre des lignes écartées ou corrigées
  20_silver.sql          tables nettoyées, dédupliquées, enrichies
  21_silver_transform.sql  les règles qualité — le cœur métier
  30_gold.sql            étoile + tables d'indicateurs, deux bases séparées
  31_gold_transform.sql  dimensions, faits, puis les indicateurs agrégés
  50_droits.sql          comptes et droits — le cloisonnement
  60_ops.sql             journal d'exécution
  99_verifications.sql   requêtes d'inspection pour la console SQL

tests/                   les huit contrôles et démonstrations
exploration/             profilage initial des sources (DuckDB)
ops/crontab.example      planification
docs/                    rapport et documentation
```

---

## Choix structurants

Détaillés et justifiés dans [`docs/RAPPORT.md`](docs/RAPPORT.md).

- **La pseudonymisation a lieu pendant la copie**, ligne par ligne. Les
  identités ne sont écrites nulle part, pas même dans un répertoire temporaire.
- **ClickHouse lit les fichiers lui-même** (`file()` sur un montage en lecture
  seule). Python n'envoie que du SQL : aucune donnée ne transite par sa mémoire.
- **Le cloisonnement est physique** — deux bases, deux comptes, droits disjoints,
  posés colonne par colonne. Le refus est prononcé par le moteur : aucun outil
  placé au-dessus de l'entrepôt ne peut le contourner.
- **Les indicateurs sont des tables, pas des vues.** En ClickHouse une vue
  s'exécute avec les droits de l'appelant : une vue gold obligerait à ouvrir
  l'accès à silver et ferait tomber le cloisonnement.
