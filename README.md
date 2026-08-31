# Entrepôt de Données de Santé — CHU

Pipeline complet d'un EDS : collecte quotidienne des dépôts du CHU,
pseudonymisation à l'ingestion, transformation en couches, restitution par
tableaux de bord cloisonnés.

**Tout s'exécute en local, en deux commandes.** Aucune étape manuelle : la
configuration de l'entrepôt et des tableaux de bord est entièrement scriptée.

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
   silver               nettoyé, dédupliqué, enrichi  +  table des rejets
        ▼
   gold_pilotage                          gold_recherche
   modèle en étoile                       agrégats · k ≥ 5
   fact_sejour · fact_diagnostic          coh_prevalence
   fact_releve                            coh_description
   dim_patient · dim_service · dim_cim10
        │                                        │
        └──── deux bases, deux comptes, droits disjoints ────┘
        ▼                                        ▼
   Dashboard pilotage                     Dashboard recherche
```

---

## Prérequis

| | |
|---|---|
| Docker Desktop | démarré (`docker info` doit répondre) |
| Python | 3.11 ou plus |
| Ports libres | `8123` ClickHouse · `3000` Metabase |
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
# 1. Secrets — génère un sel de 256 bits et trois mots de passe
cp .env.example .env
python3 - <<'EOF'
import secrets, pathlib
pathlib.Path(".env").write_text(f"""CH_ADMIN_USER=eds_admin
CH_ADMIN_PASSWORD={secrets.token_urlsafe(24)}
CH_PILOTAGE_PASSWORD={secrets.token_urlsafe(24)}
CH_RECHERCHE_PASSWORD={secrets.token_urlsafe(24)}
EDS_PSEUDO_SALT={secrets.token_hex(32)}
MB_ADMIN_EMAIL=admin@eds-chu.local
MB_ADMIN_PASSWORD={secrets.token_urlsafe(16)}
""")
EOF

# 2. Environnement Python
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 3. Entrepôt et restitution
docker compose up -d

# 4. Pipeline complet
.venv/bin/python -m eds.run
```

Comptez une quinzaine de secondes pour le premier démarrage de Metabase, puis
**environ 1,5 seconde** pour l'intégralité du pipeline.

### Accès

| | |
|---|---|
| Tableaux de bord | http://localhost:3000 |
| Console SQL | http://localhost:8123/play — utilisateur `eds_admin` |

**Trois rôles, trois vocations distinctes.** Le sujet définit deux publics
métier — « pilotage et recherche ne voient pas les mêmes données → droits
d'accès distincts ». S'y ajoute l'administration de l'entrepôt, rôle à part
entière dans un EDS : c'est elle qui exploite le pipeline, accorde les
habilitations et répond d'une demande d'effacement.

| Compte | Vocation | Ce qu'il peut faire |
|---|---|---|
| `pilotage@eds-chu.local` | **Direction hospitalière** — piloter l'activité et la qualité des soins | Consulter le tableau Pilotage. **Rien d'autre** : aucune base accessible, aucune requête possible |
| `recherche@eds-chu.local` | **Recherche clinique** — décrire des cohortes | Consulter le tableau Recherche. **Rien d'autre** |
| `admin@eds-chu.local` | **Administration de l'entrepôt** — exploiter, habiliter, tracer, assurer la conformité | Tout : les deux tableaux, les deux bases, la composition de requêtes, et les couches techniques via la console SQL |

Mots de passe : `MB_PILOTAGE_PASSWORD`, `MB_RECHERCHE_PASSWORD`, `MB_ADMIN_PASSWORD`
dans `.env`.

> **Un utilisateur métier consomme des indicateurs, il n'interroge pas
> l'entrepôt.** Les comptes de pilotage et de recherche n'ont ni éditeur SQL ni
> générateur de requêtes : dans Metabase, ils ne voient même aucune base de
> données. Sans cette restriction, un utilisateur du pilotage pourrait lire
> `fact_sejour` ligne par ligne, avec le pseudonyme patient — bien au-delà de son
> besoin.
>
> L'administrateur est le **seul** à atteindre le détail des couches bronze et
> silver, ce qui correspond à sa vocation : lui seul a besoin de remonter à la
> ligne d'origine pour traiter un incident ou une demande d'effacement. Il
> dispose pour cela d'une troisième connexion — *EDS — Exploitation* — qui donne
> accès à `bronze`, `silver` et au journal `ops`.

**Quatre comptes ClickHouse, un par usage.** Ce sont des comptes de service, à ne
pas confondre avec les trois rôles ci-dessus.

| Compte ClickHouse | Usage | Droits |
|---|---|---|
| `eds_admin` | le pipeline | tous — il crée les tables et applique les habilitations |
| `eds_pilotage` | connexion Metabase Pilotage | `SELECT` sur `gold_pilotage` |
| `eds_recherche` | connexion Metabase Recherche | `SELECT` sur `gold_recherche` |
| `eds_exploitation` | connexion Metabase Exploitation | `SELECT` sur `bronze`, `silver`, `ops` — **lecture seule** |

> Le compte d'investigation n'est **pas** celui du pipeline. `eds_admin` peut
> créer et supprimer des bases : ce pouvoir n'a pas sa place derrière une
> interface web. `eds_exploitation` ne peut rien écrire, quelle que soit la
> requête saisie — le moteur refuse.

La séparation joue à trois niveaux : permissions de collection (quel tableau est
visible), permissions de données (quelle base est interrogeable), et surtout
**droits ClickHouse** — chaque connexion utilise un compte distinct qui n'a de
`GRANT` que sur sa base. C'est le moteur qui refuse, et aucun réglage de
Metabase ne peut contourner cela.

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

Quatre contrôles, exécutables à tout moment. Ils constituent la démonstration
des propriétés annoncées.

```bash
.venv/bin/python -m tests.verifier_pseudonymisation   # aucune identité dans le lake
.venv/bin/python -m tests.verifier_qualite            # bronze = silver + rejets
.venv/bin/python -m tests.demontrer_cloisonnement     # droits d'accès disjoints
.venv/bin/python -m tests.demontrer_reprise           # erreurs et reprise sur incident
```

| Contrôle | Ce qu'il prouve |
|---|---|
| `verifier_pseudonymisation` | Les 17 503 valeurs identifiantes de la source sont introuvables dans le lake ; aucune collision de pseudonyme ; les jointures survivent |
| `verifier_qualite` | Équation de conservation par source, déduplication, règles métier, intégrité référentielle — 15 contrôles |
| `demontrer_cloisonnement` | Chaque compte accède à sa base et se voit refuser les trois autres, par le moteur |
| `demontrer_reprise` | Erreurs détectées, tracées, entrepôt cohérent, reprise par simple relance |

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
| Tableaux de bord vides | Metabase configuré avant le premier chargement | `.venv/bin/python -m eds.metabase` |
| Restitution en échec, entrepôt intact | Metabase indisponible | Idem — l'entrepôt reste valide |

**Repartir de zéro** (destructif, l'entrepôt est reconstruit intégralement) :

```bash
docker compose down -v && docker compose up -d
.venv/bin/python -m eds.run --tout
```

---

## Organisation du dépôt

```
docker-compose.yml       ClickHouse 25.8 + Metabase v0.56.13, versions épinglées
requirements.txt         2 dépendances Python

eds/                     le pipeline
  config.py              chemins et secrets, sans dépendance externe
  pseudo.py              pseudonymisation : HMAC-SHA256 salé, généralisation
  lake.py                copie transformante en flux
  warehouse.py           client ClickHouse, exécution SQL, chargement bronze
  journal.py             journalisation JSON + console
  run.py                 orchestrateur — point d'entrée
  metabase.py            configuration automatisée de la restitution
  dashboards.py          définition déclarative des tableaux de bord

sql/                     toute la transformation, versionnée
  00_databases.sql       les cinq bases
  10_bronze.sql          tables typées, partitionnées
  20_silver.sql          tables nettoyées + table des rejets
  21_silver_transform.sql  les règles qualité — le cœur métier
  30_gold.sql            indicateurs, deux bases séparées
  31_gold_transform.sql  définition des six indicateurs
  50_droits.sql          comptes et droits — le cloisonnement
  60_ops.sql             journal d'exécution
  99_verifications.sql   requêtes d'inspection pour la console SQL

tests/                   les quatre démonstrations
exploration/             profilage initial des sources (DuckDB)
metabase/dashboards.json export des tableaux de bord
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
- **Le cloisonnement est physique** — deux bases, deux comptes, droits disjoints.
  Le refus est prononcé par le moteur, y compris depuis Metabase.
- **Les indicateurs sont des tables, pas des vues.** En ClickHouse une vue
  s'exécute avec les droits de l'appelant : une vue gold obligerait à ouvrir
  l'accès à silver et ferait tomber le cloisonnement.
