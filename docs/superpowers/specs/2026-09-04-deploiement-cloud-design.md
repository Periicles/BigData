# Déploiement cloud de l'entrepôt — conception

Date : 2026-09-04 · Branche : `feat/cloud`

## 1. Objectif

Faire tourner l'entrepôt EDS CHU sur Azure, à l'identique du local, pour une
démonstration jetable : l'infrastructure se crée en une commande, le pipeline
s'exécute, les tableaux de bord se consultent, puis tout est détruit. Le
local reste le mode par défaut et ne change pas de comportement.

Contraintes retenues :

- Abonnement « Azure for Students », 85 € de crédit restant, quota de
  6 vCPU dont 4 en famille B, région France Central (certifiée HDS).
- Versions à jour, lignes LTS : ClickHouse `25.8.33.6`, Metabase
  `v0.58.32.1`, Python `3.14.7-slim-trixie`, AKS `1.35`, provider
  `azurerm` `5.4.0`. ClickHouse doit rester en 25.8 : la 26.x casse
  l'affichage des refus de droits dans Metabase.
- Aucun secret dans le dépôt, ni dans les fichiers Terraform, ni dans les
  manifestes Kubernetes.
- Les identités ne touchent jamais un disque intermédiaire : la
  pseudonymisation reste en flux, du dépôt source vers le lake.

Non-objectifs : haute disponibilité, réplication ClickHouse, ingress avec
TLS, observabilité centralisée, base Metabase Postgres. Chacun est nommé
comme limite dans le rapport, pas construit.

## 2. Architecture

```
                     Azure · France Central · rg-eds-cloud
 ┌──────────────────────────────────────────────────────────────────┐
 │  Storage Account            Key Vault             ACR (Basic)    │
 │   conteneur `source`  ─┐     secrets générés       image pipeline│
 │   conteneur `lake`    ─┤     par Terraform                       │
 │                        │        │                                │
 │  AKS 1.35 · Free · 1 nœud B2ms  │ CSI Secrets Store              │
 │  ┌──────────────────────────────▼─────────────────────────────┐  │
 │  │ namespace eds                                              │  │
 │  │  pipeline (Job / CronJob)   ClickHouse (StatefulSet)       │  │
 │  │   /data/source ◄─ blobfuse   azureBlobStorage(eds_lake)    │  │
 │  │   /data/lake   ◄─ blobfuse   PVC 10 Go · ClusterIP         │  │
 │  │                              Metabase (Deployment)         │  │
 │  │                               PVC H2 · LoadBalancer        │  │
 │  └────────────────────────────────────────────────────────────┘  │
 └──────────────────────────────────────────────────────────────────┘
```

Le pipeline lit le dépôt source et écrit le lake à travers un montage
blobfuse : `eds.lake` voit un système de fichiers, il n'a pas à connaître
Azure. ClickHouse lit le lake directement par l'API Blob, avec
`azureBlobStorage()` et une named collection portée par sa configuration :
la clé du compte de stockage n'apparaît jamais dans une requête.

## 3. Infrastructure Terraform (`infra/terraform/`)

Un seul état, local et gitignoré. Fichiers :

| Fichier | Contenu |
|---|---|
| `versions.tf` | Terraform `>= 1.10`, providers `azurerm 5.4.0`, `random 3.9.0` |
| `variables.tf` | `prefixe` (`eds`), `region` (`francecentral`), `ip_autorisee` (obligatoire, CIDR), `taille_noeud` (`Standard_B2ms`), `version_aks` (`1.35`) |
| `main.tf` | resource group, tags communs |
| `storage.tf` | Storage Account (LRS, TLS 1.2, accès public blob désactivé), conteneurs `source` et `lake` |
| `keyvault.tf` | Key Vault RBAC, `random_password` pour chaque secret, `azurerm_key_vault_secret` ; rôle « Key Vault Secrets User » à l'identité de l'addon CSI |
| `acr.tf` | ACR Basic, rôle `AcrPull` au kubelet de l'AKS |
| `aks.tf` | AKS Free, 1 nœud, identité managée, `key_vault_secrets_provider`, `storage_profile.blob_driver_enabled = true` |
| `outputs.tf` | nom du RG, du cluster, de l'ACR, du Key Vault, du Storage Account, `client_id` de l'identité CSI, `tenant_id` |
| `terraform.tfvars.example` | valeurs à copier |

Secrets générés : `ch-admin-password`, `ch-pilotage-password`,
`ch-recherche-password`, `ch-exploitation-password`, `eds-pseudo-salt`,
`mb-admin-password`, `mb-pilotage-password`, `mb-recherche-password`,
`storage-account-key` (lu depuis le Storage Account, pas généré). Les
identifiants non secrets (utilisateur admin, courriels Metabase) sont des
valeurs de ConfigMap.

Le Key Vault est créé sans protection contre la purge, pour qu'un
`terraform destroy` le retire vraiment : un coffre à purge protégée survit
90 jours et bloque une recréation au même nom.

## 4. Kubernetes (`infra/k8s/`)

Kustomize, un seul overlay. Les valeurs propres au déploiement (noms de
ressources Azure, `client_id`, `tenant_id`) sont injectées par `ops/cloud.sh`
depuis les sorties Terraform, par `kustomize edit` ou substitution, jamais
commises.

- `namespace.yaml` : `eds`.
- `secrets.yaml` : `SecretProviderClass` Azure qui synchronise les neuf
  secrets du Key Vault en un Secret Kubernetes `eds-secrets`, plus un fichier
  `lake.xml` (named collection ClickHouse) rendu depuis la clé du stockage.
- `config.yaml` : ConfigMap `eds-config` (utilisateur admin, courriels
  Metabase, `EDS_LAKE_LECTEUR=blob`, `CH_HOST=clickhouse`,
  `MB_URL=http://metabase:3000`, `EDS_SOURCE=/data/source`,
  `EDS_LAKE=/data/lake`, `MB_LOCALE=fr`, `JAVA_TIMEZONE=Europe/Paris`).
- `clickhouse.yaml` : StatefulSet 1 réplica, image `25.8.33.6`, env
  `CLICKHOUSE_USER` / `CLICKHOUSE_PASSWORD` / `CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1`,
  volume Secret monté en `/etc/clickhouse-server/config.d/lake.xml`, PVC
  10 Go `managed-csi`, `ulimits` via `securityContext`, sondes sur
  `SELECT 1`, Service ClusterIP `clickhouse` (8123, 9000).
- `metabase.yaml` : Deployment 1 réplica, image `v0.58.32.1`, mêmes
  variables que le compose (`MB_DB_TYPE=h2`, `MB_LOAD_SAMPLE_CONTENT=false`),
  PVC 2 Go, sonde `/api/health`, Service LoadBalancer avec
  `loadBalancerSourceRanges: [ip_autorisee]`.
- `pipeline.yaml` : volumes blobfuse `source` (lecture seule) et `lake`
  (lecture-écriture) déclarés en PV/PVC statiques sur le driver `blob.csi.azure.com`
  avec le Secret de la clé ; trois charges :
  - Job `eds-charger` : `python -m eds.run --tout`, `backoffLimit: 3` ;
  - Job `eds-restituer` : `python -m eds.restitution` ;
  - CronJob `eds-nuit` : `10 3 * * *`, `python -m eds.run`,
    `concurrencyPolicy: Forbid`, `backoffLimit: 3`,
    `successfulJobsHistoryLimit: 3`, `failedJobsHistoryLimit: 3`.

Pourquoi le CronJob n'appelle pas `eds.supervision` : le verrou et la relance
que ce module apporte à `cron` sont des propriétés natives du CronJob
(`concurrencyPolicy`, `backoffLimit`), et son alerte par fichier n'a pas de
sens dans un pod éphémère. La supervision reste la bonne réponse pour le
poste local ; en cluster, c'est l'orchestrateur qui la fournit.

Pourquoi H2 pour Metabase : c'est une démonstration jetable, la base
applicative de Metabase vit sur un PVC et ne contient que la configuration
que `eds.restitution` reconstruit de toute façon. Une instance durable
passerait sur Azure Database for PostgreSQL ; c'est noté en limite.

## 5. Image du pipeline (`infra/Dockerfile`)

`python:3.14.7-slim-trixie`, utilisateur non root, `requirements.txt`
seulement, copie de `eds/` et `sql/`. Pas de données dans l'image.
Construite par `az acr build --platform linux/amd64`, ce qui évite le
croisement d'architecture depuis un Mac ARM et n'exige pas Docker local.

## 6. Code Python

Le local ne change pas de comportement : chaque nouveauté est une variable
d'environnement dont l'absence conserve la valeur actuelle.

- `eds/config.py` : `SOURCE` et `LAKE` lisent `EDS_SOURCE` et `EDS_LAKE`
  si définis. Nouvelle fonction `lecteur_lake()` qui renvoie `fichier`
  (défaut) ou `blob`, toute autre valeur refusée à la frontière, comme
  `langue_metabase()`.
- `eds/warehouse.py` : `client()` lit `CH_HOST` (défaut `localhost`) et
  `CH_PORT` (défaut `8123`). Nouvelle fonction `source_lake(chemin, format,
  structure)` qui renvoie l'expression de table et l'expression de
  provenance selon le lecteur :
  - `fichier` : `file('lake/<chemin>', <format>, '<structure>')` et
    `replaceOne(_path, '/var/lib/clickhouse/user_files/', '')`, inchangé ;
  - `blob` : `azureBlobStorage(eds_lake, blob_path='<chemin>',
    format='<format>', structure='<structure>')` et le chemin littéral
    `'lake/<chemin>'`, pour que la colonne de provenance ait la même forme
    dans les deux modes.
  Les cinq chargeurs et `charger_referentiels` passent par cette fonction.
- `eds/restitution.py` : `MB_URL` lu depuis l'environnement, défaut
  `http://localhost:3000`.

Le nom de la named collection, `eds_lake`, est une constante partagée par
`warehouse.py` et le manifeste ClickHouse.

## 7. Script d'exploitation (`ops/cloud.sh`)

Quatre sous-commandes, chacune idempotente :

| Commande | Ce qu'elle fait |
|---|---|
| `deployer` | `terraform apply`, `az acr build`, `az aks get-credentials`, upload de `eds-chu-sujet/source-filestorage` vers le conteneur `source`, rendu des valeurs Terraform dans Kustomize, `kubectl apply -k`, attente de ClickHouse |
| `charger` | supprime puis recrée le Job `eds-charger`, suit ses logs |
| `restituer` | idem pour `eds-restituer`, puis affiche l'URL publique de Metabase et les identifiants depuis le Key Vault |
| `detruire` | `kubectl delete -k` (libère l'IP publique), `terraform destroy` |

Le script s'arrête à la première erreur (`set -euo pipefail`), vérifie la
présence de `az`, `terraform`, `kubectl`, et refuse de continuer sans
connexion `az` active.

## 8. Tests

Unitaires, hors ligne, dans la suite existante :

- `tests/test_config.py` : `EDS_SOURCE`/`EDS_LAKE` surchargent, absence
  conserve la valeur ; `lecteur_lake()` accepte `fichier` et `blob`, refuse
  le reste.
- `tests/test_warehouse.py` : `source_lake()` produit `file()` en mode
  fichier et `azureBlobStorage(eds_lake, …)` en mode blob ; les deux
  expressions de provenance donnent la même forme `lake/<chemin>` ; un
  chemin ou une structure contenant une quote est refusé.
- `tests/test_restitution.py` (nouveau, minimal) : `MB_URL` surchargeable.

Vérification de bout en bout, contre le cluster : `tests/verifier.py`
existant, lancé depuis un pod du pipeline (`kubectl exec`), prouve
l'équation de conservation et le refus par le moteur en cloud comme en
local. Ce n'est pas un test pytest, il reste hors de la suite.

## 9. Documentation

- `README.md` : section « Déploiement cloud », les quatre commandes, le coût,
  ce qui diffère du local.
- `docs/RAPPORT.md` : Partie 4 « Déploiement cloud » : choix (AKS contre
  Container Apps, Blob contre volume partagé, CronJob contre supervision,
  H2), schéma, sécurité (secrets, ClickHouse jamais public, IP restreinte,
  HDS), limites et coût.

## 10. Coût et cycle de vie

Estimation cluster allumé, France Central : nœud B2ms ≈ 2 €/jour, Standard
Load Balancer ≈ 0,6 €/jour, ACR Basic ≈ 0,15 €/jour, disques et stockage
quelques centimes. Soit ≈ 2,8 €/jour, ≈ 0,4 € pour une démonstration de
trois heures. `ops/cloud.sh detruire` ramène le coût à zéro ; rien ne
subsiste hors du dépôt.
