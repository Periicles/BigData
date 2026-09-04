# Entrepôt de Données de Santé — CHU

Projet fil rouge · Module Big Data · M2 · Épreuve E05 (BC05, compétences C27 → C31)

Construction d'un entrepôt de données de santé pour un CHU, depuis le dépôt
quotidien de fichiers hétérogènes jusqu'aux tableaux de bord de pilotage et de
recherche clinique, avec une chaîne de traitement automatisée et conforme au
RGPD.

**Tout s'exécute en local, en quatre commandes.** Aucune étape manuelle : la
construction de l'entrepôt, l'attribution des droits et les trois tableaux de
bord sont entièrement scriptés — rien à cliquer, rien à régler à la main.

---

## Ce que fait le pipeline

```
source-filestorage/     dépôt du CHU, lecture seule (identités en clair)
        │
        │  pseudonymisation en flux — les identités ne touchent jamais le disque
        │  projection — seules les colonnes déclarées sont écrites
        ▼
   lake/                copie pseudonymisée, réduite au nécessaire
        ▼
   bronze               tables typées, partitionnées par jour de dépôt
        ▼
   silver               nettoyé, dédupliqué, enrichi
        │  └──────────►  quarantaine     chaque ligne écartée, avec son motif
        ▼
   gold_pilotage                          gold_recherche
   13 tables d'indicateurs                agrégats · k ≥ 5
   dms_service · urgences_jour            coh_prevalence
   readmission_service · alertes_jour     coh_description
   occupation_jour · mortalite_service
   casemix_service · origine_service
   activite_categorie · actes_service
   actes_type · densite_actes_lit
   facturation_service
     ╰─ dérivées du modèle en étoile
        fact_sejour · fact_diagnostic · fact_releve · fact_acte
        dim_patient · dim_service · dim_cim10 · dim_ccam
        │                                        │
        └──── deux bases, deux comptes, droits disjoints ────┘
             le refus est prononcé par le moteur, pas par l'applicatif
        │                                        │
        ▼                                        ▼
   « Pilotage hospitalier »                « Recherche clinique »
   « Activité technique et facturation »   tableau de bord Metabase
   tableaux de bord Metabase               connexion eds_recherche
   connexion eds_pilotage
     chacun branché sur la connexion ClickHouse bornée de son usage —
             Metabase hérite du refus, il ne peut pas le contourner
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

## Les données

Le CHU dépose ses fichiers dans un espace en lecture seule. Six flux, trois
formats, et **chacun son calendrier** — c'est le point qui décide de la façon
dont on les découvre : parcourir les dates d'un flux pour en lire un autre n'en
lirait qu'une partie, sans erreur visible.

| Flux | Format | Dépôts | Période | Volume |
|---|---|---:|---|---:|
| `sejours/<date>/sejours.csv` | CSV | 28 | 01 → 28 août | 6 797 séjours |
| `diagnostics/<date>/diagnostics.json` | JSON imbriqué | 28 | 01 → 28 août | 12 720 codes CIM-10 |
| `monitoring/<date>/monitoring.parquet` | Parquet | 28 | 01 → 28 août | 41 778 relevés |
| `patients/<date>/patients.csv` | CSV | 3 | 26 → 28 août | 6 000 patients, en instantanés |
| `actes/<date>/actes.parquet` | Parquet | 1 | 29 août | 8 112 actes médicaux |
| `referentiels/<date>/*.csv` | CSV | 2 | 01 et 29 août | 8 services, 13 codes CIM-10, 8 actes CCAM, 7 services décrits |

**Les référentiels sont résolus par FICHIER, pas par jour de dépôt.** Chacun
est chargé depuis le dépôt le plus récent qui le fournit : `services.csv` et
`cim10.csv` viennent du 1er août, `ccam.csv` et `description_service.csv` du
29. Un référentiel redéposé remplace donc sa version précédente au lieu d'être
ignoré.

> **Les données ne sont pas dans ce dépôt, volontairement.** Les fichiers
> `patients.csv` contiennent des identités en clair (nom, prénom, NIR). Les
> versionner ferait entrer des données de santé nominatives dans l'historique
> Git, où elles resteraient — un commit ne se retire pas. Le répertoire
> `eds-chu-sujet/source-filestorage/` est donc exclu par `.gitignore` : placez-y
> le dépôt fourni par le CHU avant de lancer le pipeline.

---

## Prérequis

| | |
|---|---|
| Docker Desktop | démarré (`docker info` doit répondre) |
| Python | 3.11 ou plus |
| Ports libres | `8123` ClickHouse, `3000` Metabase |
| Données source | `eds-chu-sujet/source-filestorage/` — voir ci-dessus |

Les images sont **épinglées à une version exacte** dans `docker-compose.yml` —
`clickhouse-server:25.8` et `metabase:v0.58.32` — pour que deux clones du dépôt
pris à deux dates donnent le même environnement. ClickHouse reste en 25.8
délibérément : la 26.x renvoie les refus de droits en HTTP 403, statut sur
lequel le pilote embarqué dans Metabase ne lit plus le message d'erreur, et la
démonstration du cloisonnement au niveau de l'interface ne passerait plus.

Le pipeline n'a que **deux dépendances Python** (`requirements.txt`). `pytest`
vit à part, dans `requirements-dev.txt` : ce qui sert à tester n'alourdit pas ce
qui fait tourner.

---

## Démarrage

```bash
# 1. Secrets — génère le sel de 256 bits et tous les mots de passe
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

# ── Metabase (restitution, § 1 du sujet) ──
MB_ADMIN_EMAIL=admin@eds-chu.local
MB_ADMIN_PASSWORD={mdp()}
MB_PILOTAGE_EMAIL=pilotage@eds-chu.local
MB_PILOTAGE_PASSWORD={mdp()}
MB_RECHERCHE_EMAIL=recherche@eds-chu.local
MB_RECHERCHE_PASSWORD={mdp()}
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

```bash
# 5. Restitution (Partie 1 du sujet) — provisionne Metabase par son API
.venv/bin/python -m eds.restitution
```

`gold` est l'objet du dossier : deux bases, un modèle en étoile, des agrégats
anonymisés, un cloisonnement prononcé par le moteur. `eds.restitution` s'y
rebranche sans rien changer en amont — deux connexions ClickHouse bornées,
deux groupes, deux comptes applicatifs, un graphe de permissions et les deux
tableaux de bord, tous posés par l'API Metabase, jamais à la souris (voir
`eds/restitution.py`).

> **L'instance ne contient que le projet.** Metabase livre d'office une base
> de démonstration et un tableau de bord « E-commerce Insights » : la
> variable `MB_LOAD_SAMPLE_CONTENT` les empêche d'être chargés, et
> `eds.restitution` retire ceux qu'une instance plus ancienne aurait déjà
> créés. Le tri se fait sur le drapeau `is_sample` posé par Metabase, jamais
> sur un libellé : aucun objet du projet ne peut être atteint par cette
> purge.

**Metabase (une JVM) met environ une minute à répondre au tout premier
démarrage** — `docker compose ps` doit afficher `healthy` (ou
`curl -s http://localhost:3000/api/health` répondre `{"status":"ok"}`) avant
de lancer le provisioning ; `eds.restitution` attend lui-même cette santé
avant d'agir, mais patienter évite un premier passage plus long. Le
provisioning lui-même, une fois Metabase démarré, **prend environ 6 secondes
au premier passage** — il crée tout : connexions, synchronisation des schémas,
comptes, droits, 31 questions et la mise en page des trois tableaux de bord.
Les exécutions suivantes, qui ne font que réconcilier l'existant, **prennent
environ 1,2 seconde**.

### Accès

| | |
|---|---|
| Console SQL | http://localhost:8123/play — utilisateur `eds_admin` |
| Metabase | http://localhost:3000 — trois comptes : `admin@eds-chu.local`, `pilotage@eds-chu.local`, `recherche@eds-chu.local` (variables `MB_*` de `.env`) |

**Quatre comptes ClickHouse, un par usage.** Le sujet définit deux publics
métier — « pilotage et recherche ne voient pas les mêmes données → droits
d'accès distincts ». S'y ajoute l'administration de l'entrepôt, rôle à part
entière dans un EDS : c'est elle qui exploite le pipeline, accorde les
habilitations et répond d'une demande d'effacement. Chacun de ces usages a son
compte, et aucun n'a plus de droits que son besoin.

| Compte ClickHouse | Usage | Droits |
|---|---|---|
| `eds_admin` | le pipeline | tous — il crée les tables et applique les habilitations |
| `eds_pilotage` | **Direction hospitalière** — piloter l'activité et la qualité des soins | `SELECT` sur les **13 tables d'indicateurs**, plus **25 colonnes** des faits — ni `patient_pseudo`, ni `stay_id` |
| `eds_recherche` | **Recherche clinique** — décrire des cohortes | `SELECT` sur les colonnes des deux tables de cohortes |
| `eds_exploitation` | **Investigation technique** — incident, piste d'audit, effacement | `SELECT` sur `bronze`, `silver`, `quarantaine`, `ops` — **lecture seule** |

Mots de passe dans `.env` — `CH_*_PASSWORD`. Aucun n'est écrit en dur dans le
code.

**Ce sont ces deux comptes, `eds_pilotage` et `eds_recherche`, que Metabase
emploie.** Chacune des deux connexions posées par `eds.restitution` s'authentifie
directement avec le compte ClickHouse borné de son usage — jamais avec
`eds_admin` — c'est ce qui fait tenir le cloisonnement jusque dans
l'interface (détails dans `docs/RAPPORT.md`, § 7.4).

**Les trois comptes Metabase sont provisionnés par `eds.restitution`, pas à la
main.** Chaque compte applicatif (`pilotage@eds-chu.local`,
`recherche@eds-chu.local`) n'appartient qu'à son groupe et n'ouvre que son
tableau de bord — l'administrateur seul voit les deux. Mots de passe dans
`.env` — `MB_*_PASSWORD`.

> **Limite résiduelle de l'édition gratuite.** `GET /api/database` reste
> visible pour un compte métier avec le NOM et l'id de la connexion de
> l'autre usage (masquer un nom de base — `"view-data": "blocked"` — exige un
> jeton premium). Le contenu, lui, est bien bloqué à tous les niveaux
> vérifiés : aucune table de la base étrangère n'est synchronisée pour ce
> compte, toute requête dessus est refusée par Metabase lui-même, et son
> tableau de bord répond HTTP 403. Voir `tests.demontrer restitution`.

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

---

## Vérifier

Trois niveaux, exécutables à tout moment, et aucun ne remplace les deux
autres. Les **tests unitaires** s'exercent sur les fonctions pures : hors ligne,
sans Docker, en une fraction de seconde. `tests.verifier` confronte l'entrepôt
vivant à lui-même — chaque indicateur au recalcul depuis le fait dont il sort.
`tests.demontrer` écrit dedans, injecte des lignes fautives, puis le remet en
état : il prouve que les règles FONCTIONNENT, y compris celles qu'aucune donnée
réelle n'exerce.

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest              # 102 tests unitaires, 0,1 s, hors ligne
```

```bash
.venv/bin/python -m tests.verifier      # 459 contrôles, cinq sections
.venv/bin/python -m tests.demontrer     # les cinq démonstrations

# ou une section à la fois
.venv/bin/python -m tests.verifier pseudonymisation   # aucune identité dans le lake
.venv/bin/python -m tests.verifier qualite            # bronze = silver + quarantaine
.venv/bin/python -m tests.verifier indicateurs        # les indicateurs du §4
.venv/bin/python -m tests.verifier rgpd               # les 5 contraintes du sujet
.venv/bin/python -m tests.verifier conformite         # confrontation aux valeurs de l'intervenant
.venv/bin/python -m tests.demontrer cloisonnement     # droits d'accès disjoints (le moteur)
.venv/bin/python -m tests.demontrer restitution       # le même cloisonnement, vu de l'interface
.venv/bin/python -m tests.demontrer reprise           # erreurs et reprise sur incident
.venv/bin/python -m tests.demontrer qualite           # les contrôles face à des lignes fautives
.venv/bin/python -m tests.demontrer effectifs         # le seuil des 5 patients, de part et d'autre
```

| Contrôle | Ce qu'il prouve |
|---|---|
| `verifier pseudonymisation` | Les 17 384 valeurs identifiantes de la source sont introuvables dans le lake ; aucune collision de pseudonyme ; les jointures survivent |
| `verifier qualite` | Équation de conservation par source, déduplication, règles métier du §3, intégrité référentielle de silver **et** du modèle en étoile — 53 contrôles |
| `verifier indicateurs` | Les indicateurs du §4, calculés depuis gold : leur **valeur restituée** et la propriété qui la fonde — dénominateur de la DMS, inclusion numérateur/dénominateur de la réadmission (brute et ajustée), seuils d'alerte effectivement issus de la configuration, coïncidence de chaque table agrégée avec le fait dont elle sort — 50 contrôles |
| `verifier rgpd` | Les cinq contraintes RGPD, vérifiées sur l'entrepôt réel : pseudonymisation, minimisation, cloisonnement, petits effectifs, traçabilité — plus l'absence de donnée personnelle dans les journaux |
| `verifier conformite` | Confrontation directe de l'entrepôt aux valeurs de référence fournies par l'intervenant — silver et les six indicateurs — comptages exacts, moyennes à ±0,1. Fichier de référence **local, non versionné** : la section s'ignore, plutôt que d'échouer, s'il est absent |
| `demontrer cloisonnement` | Chaque compte accède à sa base et se voit refuser les trois autres, par le moteur |
| `demontrer restitution` | Le même cloisonnement, prouvé cette fois contre l'API Metabase : chaque compte métier ne voit le contenu que de sa base et n'ouvre que son tableau de bord (HTTP 403 sur l'autre) ; puis, avec un compte **administrateur**, une requête native est forcée sur la mauvaise base à travers chacune des deux connexions — refusée par ClickHouse lui-même (« Not enough privileges »), jamais par un réglage Metabase contournable ; enfin, aucune des deux connexions n'atteint bronze, silver ni quarantaine |
| `demontrer reprise` | Erreurs détectées, tracées, entrepôt cohérent, reprise par simple relance |
| `demontrer qualite` | Des lignes fautives sont injectées en bronze : dates illisibles écartées, sexe hors nomenclature corrigé, casse redressée sans bruit, séjour incohérent conservé avec ses diagnostics et relevés (`sejour_coherent = 0`), `stay_id` inconnu écarté (motif `sejour_inconnu`), équation de conservation intacte — puis l'entrepôt est remis en état |
| `demontrer effectifs` | Deux cohortes sont fabriquées de part et d'autre du seuil RGPD : celle de 4 patients existe au grain du fait mais n'atteint pas la base recherche, celle de 5 passe — le filtre coupe **sous** 5, pas à 5 |

> **`demontrer restitution` a besoin de Metabase, démarré et provisionné**
> (`docker compose up -d metabase` puis `python -m eds.restitution`). S'il est
> éteint ou pas encore provisionné, la section l'annonce clairement — jamais
> une trace Python — et échoue : la propriété n'a alors pas pu être vérifiée.

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
| `Metabase ne répond pas sur /api/health après …s` | Le conteneur `metabase` n'est pas démarré, ou encore en cours de démarrage (JVM, ~1 minute la première fois) | `docker compose up -d metabase`, puis `docker compose logs metabase` pour la cause si l'attente échoue malgré tout |
| `connexions Metabase absentes` / `tableaux de bord Metabase absents` (dans `demontrer restitution`) | `eds.restitution` n'a jamais été joué contre cette instance | `.venv/bin/python -m eds.restitution` |
| `Ports are not available` / `bind: address already in use` sur `docker compose up -d metabase` | Le port `3000` est déjà occupé par un autre processus | Libérer le port (`lsof -i :3000`), ou republier Metabase sur un autre port dans `docker-compose.yml` |

**Repartir de zéro** (destructif, l'entrepôt est reconstruit intégralement) :

```bash
docker compose down -v && docker compose up -d
.venv/bin/python -m eds.run --tout
.venv/bin/python -m eds.restitution   # -v supprime aussi metabase-data : Metabase repart de zéro
```

---

---

## Conformité RGPD

Les cinq contraintes du sujet sont vérifiables en une commande
(`python -m tests.verifier rgpd`) :

| Contrainte | Mise en œuvre | Où |
|---|---|---|
| **Pseudonymisation** | HMAC-SHA256 salé appliqué **pendant la copie** : les identités ne sont écrites nulle part. Aucune colonne `nir`, `nom`, `prenom`, `birth_date` ni `patient_id` n'existe dans l'entrepôt. | `eds/lake.py` |
| **Minimisation** | Trois colonnes supprimées à la source, date de naissance généralisée à l'année. Chaque fichier est en outre projeté sur les colonnes que `COLONNES_LAKE` déclare : ce qui n'y figure pas n'entre pas dans le lake. La base recherche n'expose ni `birth_year`, ni `patient_pseudo`, ni `region`. | `eds/config.py`, `eds/lake.py`, `sql/31_gold_transform.sql` |
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

## Choix structurants

Détaillés et justifiés dans [`docs/RAPPORT.md`](docs/RAPPORT.md).

- **La pseudonymisation a lieu pendant la copie**, ligne par ligne. Les
  identités ne sont écrites nulle part, pas même dans un répertoire temporaire.
- **Le lake ne contient que ce qui est déclaré.** Chaque source énumère, fichier
  par fichier, les colonnes qu'elle a le droit d'y déposer ; tout le reste est
  retiré avant écriture, en CSV comme en JSON ou en Parquet. Une colonne
  identifiante ajoutée demain en amont ne traverse pas, sans qu'une ligne de
  code ait à changer.
- **ClickHouse lit les fichiers lui-même** (`file()` sur un montage en lecture
  seule). Python n'envoie que du SQL : aucune donnée ne transite par sa mémoire.
- **Le cloisonnement est physique** — deux bases, deux comptes, droits disjoints,
  posés colonne par colonne. Le refus est prononcé par le moteur : aucun outil
  placé au-dessus de l'entrepôt ne peut le contourner.
- **Les indicateurs sont des tables, pas des vues.** Une table est un
  instantané : stable entre deux exécutions, comparable au fait dont elle sort
  (`tests.verifier indicateurs` le fait à chaque passage) et indépendante des
  droits de celui qui la lit. Une vue ordinaire s'exécute avec les droits de
  l'appelant et obligerait à ouvrir silver ; ClickHouse sait depuis la 24.4
  déclarer une vue `SQL SECURITY DEFINER`, mais on aurait alors un indicateur
  recalculé à chaque lecture, sans instantané à vérifier — c'est l'alternative
  écartée.

---

## Organisation du dépôt

```
docker-compose.yml       ClickHouse 25.8 et Metabase v0.58.32, versions épinglées
requirements.txt         2 dépendances — le pipeline
requirements-dev.txt     pytest — les tests unitaires, hors du chemin d'exécution

eds/                     le pipeline
  config.py              chemins, secrets, seuils et colonnes autorisées dans le lake
  lake.py                copie projetante et transformante + pseudonymisation
  warehouse.py           client ClickHouse, exécution SQL, chargement bronze
  journal.py             journalisation JSON + console
  run.py                 orchestrateur — point d'entrée
  restitution.py         provisionne Metabase par son API

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

tests/
  verifier.py            459 contrôles contre l'entrepôt vivant
  demontrer.py           cinq démonstrations, par injection puis remise en état
  test_lake.py           102 tests unitaires — fonctions pures, hors ligne
  test_warehouse.py      (pytest, sans Docker ni ClickHouse)
  test_config.py

exploration/             profilage initial des sources (DuckDB)
ops/crontab.example      planification
docs/                    le rapport, ses captures, et le script qui le rend en PDF
```

---

## Documentation

| Document | Contenu |
|---|---|
| [`docs/RAPPORT.md`](docs/RAPPORT.md) | **Le rapport de conception**, en trois parties — l'interface d'analyse, l'automatisation, l'évolution demandée par le CHU — puis la validation des chiffres et les leçons du projet. Le document à lire en premier. Également fourni en [PDF](docs/RAPPORT.pdf), sommaire compris. |
| [`exploration/RAPPORT-EXPLORATION.md`](exploration/RAPPORT-EXPLORATION.md) | L'état des lieux des sources, établi **avant** toute décision d'architecture : volumétrie, anomalies chiffrées, mesure du risque de ré-identification. |
| [`sql/99_verifications.sql`](sql/99_verifications.sql) | Requêtes d'inspection à exécuter dans la console SQL, commentées. |

`docs/RAPPORT.md` fait foi ; `docs/RAPPORT.pdf` en est le rendu de lecture,
sommaire, diagrammes et captures compris.
