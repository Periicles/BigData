# Déploiement cloud — plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Faire tourner l'entrepôt EDS CHU sur Azure (AKS + Blob + Key Vault, provisionnés par Terraform) pour une démonstration jetable, sans changer le comportement local.

**Architecture:** Terraform crée le socle Azure (RG, Storage Account avec conteneurs `source` et `lake`, Key Vault avec secrets générés, ACR, AKS 1 nœud avec addons Key Vault CSI et Blob CSI). Kubernetes porte ClickHouse (StatefulSet, lit le lake par `azureBlobStorage()` et une named collection), Metabase (Deployment, LoadBalancer restreint à une IP) et le pipeline (Job initial, Job restitution, CronJob nocturne) qui voit `source` et `lake` comme des répertoires grâce à blobfuse. Le code Python gagne des surcharges par variable d'environnement dont l'absence conserve le comportement actuel.

**Tech Stack:** Terraform 1.16 · azurerm 5.4.0 · random 3.9.0 · time 0.14.1 · AKS 1.35 · ClickHouse 25.8.33.6 · Metabase v0.58.32.1 · Python 3.14.7-slim-trixie · blob.csi.azure.com · secrets-store.csi.x-k8s.io/v1

**Spec:** `docs/superpowers/specs/2026-09-04-deploiement-cloud-design.md`

## Global Constraints

- ClickHouse reste sur la ligne `25.8` : image `clickhouse/clickhouse-server:25.8.33.6`. Jamais 26.x.
- Metabase reste sur la ligne LTS `0.58` : image `metabase/metabase:v0.58.32.1`.
- Image du pipeline : `python:3.14.7-slim-trixie`, utilisateur non root, aucune donnée dans l'image.
- Providers Terraform épinglés : `azurerm = "5.4.0"`, `random = "3.9.0"`, `time = "0.14.1"`. `required_version = ">= 1.10"`.
- Région `francecentral`, nœud `Standard_B2ms`, AKS `1.35`, tier `Free`.
- Aucun secret dans git : ni `.tfvars`, ni état Terraform, ni manifeste rendu. Les seuls secrets vivent dans Key Vault, générés par Terraform.
- Le local ne change pas : chaque nouvelle variable d'environnement a une valeur par défaut égale au comportement d'aujourd'hui.
- Commits en français, type conventionnel (`feat`, `fix`, `docs`, `chore`, `test`), sans trailer d'attribution.
- Le nom de la named collection ClickHouse est `eds_lake`, partagé par `eds/warehouse.py` et le manifeste ClickHouse.
- Les tests unitaires (`.venv/bin/python -m pytest`) doivent rester verts et hors ligne après chaque tâche.

---

## Fichiers

| Action | Fichier | Responsabilité |
|---|---|---|
| Modifier | `eds/config.py` | `chemin_depuis_env`, `SOURCE`/`LAKE` surchargeables, `lecteur_lake()`, `url_metabase()` |
| Modifier | `eds/warehouse.py` | `client()` avec `CH_HOST`/`CH_PORT`, `source_lake()`, chargeurs réécrits dessus |
| Modifier | `eds/run.py` | sonde d'accès au lake via `source_lake()` |
| Modifier | `eds/restitution.py` | `MB_URL` depuis `config.url_metabase()` |
| Modifier | `tests/test_config.py`, `tests/test_warehouse.py` | tests des surcharges et du générateur |
| Créer | `infra/Dockerfile`, `.dockerignore` | image du pipeline |
| Créer | `infra/terraform/{versions,variables,main,storage,keyvault,acr,aks,outputs}.tf`, `lake.xml.tftpl`, `terraform.tfvars.example` | socle Azure |
| Créer | `infra/k8s/base/{kustomization,namespace,secrets,config,storage,clickhouse,metabase,cronjob,job-charger,job-restituer}.yaml` | manifestes avec marque-places `__X__` |
| Créer | `ops/cloud.sh` | `deployer`, `charger`, `restituer`, `etat`, `detruire` |
| Modifier | `.gitignore` | état Terraform, tfvars, manifestes rendus |
| Modifier | `README.md`, `docs/RAPPORT.md` | section et partie « Déploiement cloud » |

---

### Task 1 : chemins, lecteur du lake et URL Metabase dans `config.py`

**Files:**
- Modify: `eds/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `chemin_depuis_env(nom: str, defaut: Path) -> Path` ; `SOURCE`, `LAKE` désormais calculés avec ; `LECTEURS_LAKE = ("fichier", "blob")` ; `lecteur_lake() -> str` ; `URL_METABASE_DEFAUT = "http://localhost:3000"` ; `url_metabase() -> str`.

- [ ] **Step 1 : écrire les tests qui échouent**

Ajouter à la fin de `tests/test_config.py` :

```python
# ── Chemins surchargeables ───────────────────────────────────────────────
def test_un_chemin_absent_de_l_environnement_garde_sa_valeur_par_defaut(env_vierge):
    from pathlib import Path

    defaut = Path("/defaut/lake")
    assert config.chemin_depuis_env("EDS_LAKE", defaut) == defaut


def test_un_chemin_se_surcharge_par_l_environnement(env_vierge, monkeypatch):
    from pathlib import Path

    monkeypatch.setenv("EDS_LAKE", "/data/lake")
    assert config.chemin_depuis_env("EDS_LAKE", Path("/defaut")) == Path("/data/lake")


def test_un_chemin_vide_est_traite_comme_absent(env_vierge, monkeypatch):
    from pathlib import Path

    monkeypatch.setenv("EDS_SOURCE", "   ")
    assert config.chemin_depuis_env("EDS_SOURCE", Path("/defaut")) == Path("/defaut")


# ── Lecteur du lake ──────────────────────────────────────────────────────
def test_lecteur_par_defaut_est_le_fichier(env_vierge):
    assert config.lecteur_lake() == "fichier"


@pytest.mark.parametrize("valeur, attendu", [("blob", "blob"), (" Blob ", "blob"), ("FICHIER", "fichier")])
def test_lecteur_se_surcharge_et_se_normalise(env_vierge, monkeypatch, valeur, attendu):
    monkeypatch.setenv("EDS_LAKE_LECTEUR", valeur)
    assert config.lecteur_lake() == attendu


@pytest.mark.parametrize("refuse", ["s3", "azure", "file"])
def test_lecteur_hors_liste_est_refuse(env_vierge, monkeypatch, refuse):
    monkeypatch.setenv("EDS_LAKE_LECTEUR", refuse)
    with pytest.raises(RuntimeError, match="EDS_LAKE_LECTEUR"):
        config.lecteur_lake()


# ── URL de Metabase ──────────────────────────────────────────────────────
def test_url_metabase_par_defaut_est_locale(env_vierge):
    assert config.url_metabase() == "http://localhost:3000"


def test_url_metabase_se_surcharge_sans_barre_finale(env_vierge, monkeypatch):
    monkeypatch.setenv("MB_URL", "http://metabase:3000/")
    assert config.url_metabase() == "http://metabase:3000"


@pytest.mark.parametrize("refusee", ["metabase:3000", "ftp://x", ""])
def test_url_metabase_sans_schema_http_est_refusee(env_vierge, monkeypatch, refusee):
    monkeypatch.setenv("MB_URL", refusee)
    if refusee == "":
        assert config.url_metabase() == "http://localhost:3000"
        return
    with pytest.raises(RuntimeError, match="MB_URL"):
        config.url_metabase()
```

- [ ] **Step 2 : vérifier l'échec**

Run: `.venv/bin/python -m pytest tests/test_config.py -q`
Expected: échecs `AttributeError: module 'eds.config' has no attribute 'chemin_depuis_env'` (et `lecteur_lake`, `url_metabase`).

- [ ] **Step 3 : implémenter dans `eds/config.py`**

Remplacer les deux lignes `SOURCE = …` et `LAKE = …` par :

```python
def chemin_depuis_env(nom: str, defaut: Path) -> Path:
    """Chemin lu dans l'environnement du processus, ou sa valeur par défaut.

    Lu à l'import du module, donc depuis l'environnement RÉEL — pas depuis
    `.env`, qui n'est chargé qu'à la première demande de secret. C'est
    voulu : ces chemins sont posés par la machine qui exécute (un montage
    dans un conteneur), pas par le fichier de réglages du poste.
    """
    valeur = os.environ.get(nom, "").strip()
    return Path(valeur) if valeur else defaut


# Dépôt du CHU : accès en lecture seule, jamais écrit par le pipeline.
SOURCE = chemin_depuis_env("EDS_SOURCE", RACINE / "eds-chu-sujet" / "source-filestorage")

# Zone de travail : copie pseudonymisée des dépôts.
LAKE = chemin_depuis_env("EDS_LAKE", RACINE / "lake")
```

Ajouter, après `LANGUES_METABASE`, les constantes :

```python
# ── Où ClickHouse lit le lake ───────────────────────────────────────────
#
# `fichier` : le lake est un répertoire monté dans `user_files`, lu par
# `file()` — le mode du poste et de docker-compose. `blob` : le lake est un
# conteneur Azure Blob, lu par `azureBlobStorage()` à travers la named
# collection `eds_lake` de la configuration du serveur. Le pipeline, lui,
# voit toujours un système de fichiers ; seule la lecture par le moteur
# change.
LECTEUR_LAKE_DEFAUT = "fichier"
LECTEURS_LAKE = ("fichier", "blob")

# Adresse de Metabase vue par `eds.restitution` : le port publié par
# docker-compose sur le poste, le nom du Service dans un cluster.
URL_METABASE_DEFAUT = "http://localhost:3000"
```

Ajouter, après `langue_metabase()` :

```python
def lecteur_lake() -> str:
    """Lecteur du lake côté ClickHouse, surchargeable par `EDS_LAKE_LECTEUR`."""
    _charger_env()
    valeur = os.environ.get("EDS_LAKE_LECTEUR", "").strip().lower() or LECTEUR_LAKE_DEFAUT
    if valeur not in LECTEURS_LAKE:
        raise RuntimeError(
            f"Lecteur du lake invalide : EDS_LAKE_LECTEUR={valeur!r} "
            f"(attendu : {' ou '.join(LECTEURS_LAKE)})."
        )
    return valeur


def url_metabase() -> str:
    """URL de Metabase, surchargeable par `MB_URL`, sans barre finale."""
    _charger_env()
    valeur = os.environ.get("MB_URL", "").strip() or URL_METABASE_DEFAUT
    if not valeur.startswith(("http://", "https://")):
        raise RuntimeError(
            f"URL Metabase invalide : MB_URL={valeur!r} (http:// ou https:// attendu)."
        )
    return valeur.rstrip("/")
```

- [ ] **Step 4 : vérifier le passage**

Run: `.venv/bin/python -m pytest -q`
Expected: tout vert (129 tests existants + 12 nouveaux).

- [ ] **Step 5 : commit**

```bash
git add eds/config.py tests/test_config.py
git commit -m "feat(config): chemins, lecteur du lake et URL Metabase surchargeables par l'environnement"
```

---

### Task 2 : `source_lake()` et hôte ClickHouse dans `warehouse.py`, sonde dans `run.py`

**Files:**
- Modify: `eds/warehouse.py` (fonction `client`, section « Chargement bronze », `charger_referentiels`)
- Modify: `eds/run.py` (méthode `Pipeline.verifier_acces_lake`)
- Test: `tests/test_warehouse.py`

**Interfaces:**
- Consumes: `config.lecteur_lake()` (Task 1).
- Produces: `NOM_COLLECTION_BLOB = "eds_lake"` ; `source_lake(chemin: str, format: str, structure: str = "") -> tuple[str, str]` renvoyant `(expression_table, expression_provenance)`.

- [ ] **Step 1 : écrire les tests qui échouent**

Ajouter à `tests/test_warehouse.py` :

```python
# ── Lecture du lake par le moteur ────────────────────────────────────────
def test_lecteur_fichier_produit_un_file_avec_structure(env_vierge):
    table, provenance = warehouse.source_lake(
        "patients/2026-08-26/patients.csv", "CSVWithNames", "patient_pseudo String"
    )
    assert table == "file('lake/patients/2026-08-26/patients.csv', CSVWithNames, 'patient_pseudo String')"
    assert provenance == "replaceOne(_path, '/var/lib/clickhouse/user_files/', '')"


def test_lecteur_fichier_sans_structure_laisse_le_moteur_deviner(env_vierge):
    table, _ = warehouse.source_lake("actes/2026-08-29/actes.parquet", "Parquet")
    assert table == "file('lake/actes/2026-08-29/actes.parquet', Parquet)"


def test_lecteur_blob_passe_par_la_named_collection(env_vierge, monkeypatch):
    monkeypatch.setenv("EDS_LAKE_LECTEUR", "blob")
    table, provenance = warehouse.source_lake(
        "patients/2026-08-26/patients.csv", "CSVWithNames", "patient_pseudo String"
    )
    assert table == (
        "azureBlobStorage(eds_lake, blob_path='patients/2026-08-26/patients.csv', "
        "format='CSVWithNames', structure='patient_pseudo String')"
    )
    assert provenance == "'lake/patients/2026-08-26/patients.csv'"


def test_lecteur_blob_sans_structure(env_vierge, monkeypatch):
    monkeypatch.setenv("EDS_LAKE_LECTEUR", "blob")
    table, _ = warehouse.source_lake("actes/2026-08-29/actes.parquet", "Parquet")
    assert table == "azureBlobStorage(eds_lake, blob_path='actes/2026-08-29/actes.parquet', format='Parquet')"


def test_la_provenance_a_la_meme_forme_dans_les_deux_lecteurs(env_vierge, monkeypatch):
    """Un fichier chargé en local et en cloud porte le même `_source_path`."""
    _, fichier = warehouse.source_lake("sejours/2026-08-01/sejours.csv", "CSVWithNames")
    monkeypatch.setenv("EDS_LAKE_LECTEUR", "blob")
    _, blob = warehouse.source_lake("sejours/2026-08-01/sejours.csv", "CSVWithNames")
    # En mode fichier, `_path` vaut `/var/lib/clickhouse/user_files/lake/…`,
    # d'où `lake/…` après retrait du préfixe : la même chaîne qu'en blob.
    assert blob == "'lake/sejours/2026-08-01/sejours.csv'"
    assert fichier.startswith("replaceOne(_path")


@pytest.mark.parametrize(
    "chemin",
    ["a'b.csv", "../etc/passwd", "x/../../y", "a b.csv", "", "x;y"],
)
def test_chemin_hostile_est_refuse(env_vierge, chemin):
    with pytest.raises(ValueError, match="Chemin"):
        warehouse.source_lake(chemin, "Parquet")


@pytest.mark.parametrize("format", ["Parquet'", "CSV With Names", "", "x()"])
def test_format_hostile_est_refuse(env_vierge, format):
    with pytest.raises(ValueError, match="Format"):
        warehouse.source_lake("a.csv", format)


@pytest.mark.parametrize("structure", ["a String'", "a String\\"])
def test_structure_hostile_est_refusee(env_vierge, structure):
    with pytest.raises(ValueError, match="Structure"):
        warehouse.source_lake("a.csv", "CSVWithNames", structure)


def test_la_sonde_du_lake_est_un_chemin_admis(env_vierge):
    table, _ = warehouse.source_lake(".sonde", "LineAsString")
    assert table == "file('lake/.sonde', LineAsString)"


def test_tous_les_chargeurs_passent_par_source_lake(env_vierge, monkeypatch):
    """Aucun chargeur ne garde un `file()` en dur : en blob, aucun n'en émet."""
    monkeypatch.setenv("EDS_LAKE_LECTEUR", "blob")
    for _, fabriquer_sql in warehouse.CHARGEURS.values():
        sql = fabriquer_sql("2026-08-01", "run0")
        assert "file(" not in sql
        assert "azureBlobStorage(eds_lake" in sql
        assert "_path" not in sql


# ── Hôte ClickHouse ──────────────────────────────────────────────────────
def test_hote_clickhouse_par_defaut_est_local(env_vierge):
    assert warehouse.hote_clickhouse() == ("localhost", 8123)


def test_hote_clickhouse_se_surcharge(env_vierge, monkeypatch):
    monkeypatch.setenv("CH_HOST", "clickhouse")
    monkeypatch.setenv("CH_PORT", "8124")
    assert warehouse.hote_clickhouse() == ("clickhouse", 8124)


@pytest.mark.parametrize("port", ["abc", "0", "-1", "70000"])
def test_port_clickhouse_invalide_est_refuse(env_vierge, monkeypatch, port):
    monkeypatch.setenv("CH_PORT", port)
    with pytest.raises(RuntimeError, match="CH_PORT"):
        warehouse.hote_clickhouse()
```

`env_vierge` vient de `tests/conftest.py` (déjà importé implicitement comme fixture).

- [ ] **Step 2 : vérifier l'échec**

Run: `.venv/bin/python -m pytest tests/test_warehouse.py -q`
Expected: `AttributeError: module 'eds.warehouse' has no attribute 'source_lake'` et `hote_clickhouse`.

- [ ] **Step 3 : implémenter dans `eds/warehouse.py`**

Remplacer l'import de config par :

```python
from eds.config import LAKE, RACINE, exiger, lecteur_lake
```

Remplacer le bloc `LAKE_CH = "lake"` et la fonction `client()` par :

```python
# Chemin du lake tel que ClickHouse le voit en mode `fichier` (cf. volume
# dans docker-compose). En mode `blob`, c'est le préfixe conservé dans la
# colonne de provenance pour que les deux modes écrivent la même chose.
LAKE_CH = "lake"
PREFIXE_USER_FILES = "/var/lib/clickhouse/user_files/"

# Named collection de la configuration du serveur ClickHouse (cf.
# infra/k8s/base/secrets.yaml) : elle porte la chaîne de connexion au compte
# de stockage, de sorte qu'aucune clé ne transite jamais par une requête.
NOM_COLLECTION_BLOB = "eds_lake"

HOTE_DEFAUT = "localhost"
PORT_DEFAUT = 8123

_FORMAT_JOUR = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Un chemin relatif du lake : segments alphanumériques, point, tiret, souligné.
_CHEMIN_LAKE_ADMIS = re.compile(r"[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)*")
# Un nom de format ClickHouse : `CSVWithNames`, `Parquet`, `LineAsString`…
_FORMAT_ADMIS = re.compile(r"[A-Za-z]+")


def hote_clickhouse() -> tuple[str, int]:
    """Hôte et port HTTP de ClickHouse, surchargeables par `CH_HOST` / `CH_PORT`.

    `localhost:8123` est le port publié par docker-compose sur le poste ;
    dans un cluster, c'est le nom du Service. Le port est validé ici, à la
    frontière, comme les seuils.
    """
    _charger_env()
    hote = os.environ.get("CH_HOST", "").strip() or HOTE_DEFAUT
    port_brut = os.environ.get("CH_PORT", "").strip()
    if not port_brut:
        return hote, PORT_DEFAUT
    if not port_brut.isdigit() or not 1 <= int(port_brut) <= 65535:
        raise RuntimeError(
            f"Port ClickHouse invalide : CH_PORT={port_brut!r} (entier de 1 à 65535 attendu)."
        )
    return hote, int(port_brut)


def client() -> Client:
    hote, port = hote_clickhouse()
    return clickhouse_connect.get_client(
        host=hote,
        port=port,
        username=exiger("CH_ADMIN_USER"),
        password=exiger("CH_ADMIN_PASSWORD"),
    )


def source_lake(chemin: str, format: str, structure: str = "") -> tuple[str, str]:
    """Expression de table qui lit un fichier du lake, et expression de sa provenance.

    Une seule fonction pour les deux lecteurs, afin qu'aucun chargeur ne
    connaisse Azure. Les trois arguments sont interpolés dans du SQL — les
    fonctions de table n'acceptent pas de paramètre lié — donc validés
    strictement : ni quote, ni espace, ni remontée de répertoire.

    La provenance a la même forme dans les deux modes (`lake/<chemin>`) :
    en mode fichier elle est lue dans la colonne virtuelle `_path`, en mode
    blob elle est le chemin demandé, ce qui revient au même.
    """
    if not _CHEMIN_LAKE_ADMIS.fullmatch(chemin) or ".." in chemin.split("/"):
        raise ValueError(f"Chemin du lake refusé : {chemin!r}")
    if not _FORMAT_ADMIS.fullmatch(format):
        raise ValueError(f"Format refusé : {format!r}")
    if "'" in structure or "\\" in structure:
        raise ValueError(f"Structure refusée : {structure!r}")

    if lecteur_lake() == "blob":
        arguments = [NOM_COLLECTION_BLOB, f"blob_path='{chemin}'", f"format='{format}'"]
        if structure:
            arguments.append(f"structure='{structure}'")
        return f"azureBlobStorage({', '.join(arguments)})", f"'{LAKE_CH}/{chemin}'"

    arguments = [f"'{LAKE_CH}/{chemin}'", format]
    if structure:
        arguments.append(f"'{structure}'")
    return f"file({', '.join(arguments)})", f"replaceOne(_path, '{PREFIXE_USER_FILES}', '')"
```

Les imports en tête de module deviennent : `import os` parmi les imports standard, et `from eds.config import LAKE, RACINE, _charger_env, exiger, lecteur_lake`.

Réécrire les cinq chargeurs et `charger_referentiels` :

```python
def _sql_patients(jour: str, run_id: str) -> str:
    # birth_year est lu en STRING puis converti en mode TOLÉRANT
    # (`toUInt16OrNull`) : une date de naissance illisible produit une valeur
    # vide dans le lake (cf. eds/lake.py `annee_naissance`), donc un NULL ici
    # — au lieu de faire échouer le chargement du jour ENTIER. Le patient
    # reste conservé, silver le trace en quarantaine.
    table, provenance = source_lake(
        f"patients/{jour}/patients.csv", "CSVWithNames",
        "patient_pseudo String, birth_year String, sex String, region_code String",
    )
    return f"""
    INSERT INTO bronze.patients
    SELECT patient_pseudo, toUInt16OrNull(birth_year), sex, region_code,
           toDate('{jour}'), {provenance}, now(), '{run_id}'
    FROM {table}
    """


def _sql_sejours(jour: str, run_id: str) -> str:
    # (conserver le commentaire existant sur les dates tolérantes et discharge_ts)
    table, provenance = source_lake(
        f"sejours/{jour}/sejours.csv", "CSVWithNames",
        "stay_id String, service_code String, admission_ts String, "
        "discharge_ts String, admission_mode String, discharge_mode String, "
        "patient_pseudo String",
    )
    return f"""
    INSERT INTO bronze.sejours
    SELECT stay_id, patient_pseudo, service_code,
           parseDateTimeBestEffortOrNull(admission_ts),
           parseDateTimeBestEffortOrNull(nullIf(discharge_ts, '')),
           discharge_ts != ''
               AND parseDateTimeBestEffortOrNull(discharge_ts) IS NULL,
           admission_mode, discharge_mode,
           toDate('{jour}'), {provenance}, now(), '{run_id}'
    FROM {table}
    """


def _sql_diagnostics(jour: str, run_id: str) -> str:
    # Aplatissement du JSON imbriqué par ARRAY JOIN, dans le moteur.
    table, provenance = source_lake(
        f"diagnostics/{jour}/diagnostics.json", "JSONEachRow",
        "stay_id String, diagnostics Array(Tuple(code_cim10 String, type String))",
    )
    return f"""
    INSERT INTO bronze.diagnostics
    SELECT stay_id, d.code_cim10, d.type,
           toDate('{jour}'), {provenance}, now(), '{run_id}'
    FROM {table}
    ARRAY JOIN diagnostics AS d
    """


def _sql_monitoring(jour: str, run_id: str) -> str:
    table, provenance = source_lake(f"monitoring/{jour}/monitoring.parquet", "Parquet")
    return f"""
    INSERT INTO bronze.monitoring
    SELECT stay_id, ts, toInt16(heart_rate), toInt16(spo2), toDecimal32(temp_c, 1),
           toDate('{jour}'), {provenance}, now(), '{run_id}'
    FROM {table}
    """


def _sql_actes(jour: str, run_id: str) -> str:
    # (conserver le commentaire existant : timestamp typé, aucune identité)
    table, provenance = source_lake(f"actes/{jour}/actes.parquet", "Parquet")
    return f"""
    INSERT INTO bronze.actes
    SELECT stay_id, code_ccam, acte_ts,
           toDate('{jour}'), {provenance}, now(), '{run_id}'
    FROM {table}
    """
```

Dans `charger_referentiels`, remplacer l'`INSERT` par :

```python
        table_source, provenance = source_lake(
            f"referentiels/{jour}/{fichier}", "CSVWithNames", colonnes
        )
        ch.command(f"TRUNCATE TABLE {table}")
        ch.command(f"""
            INSERT INTO {table}
            SELECT {projection}, {provenance}, now(), '{run_id}'
            FROM {table_source}
        """)
```

Mettre à jour la docstring de module : « ClickHouse lit lui-même les fichiers du lake — montés dans `user_files` sur le poste, ou depuis un conteneur Azure Blob en cloud (cf. `source_lake`). »

- [ ] **Step 4 : adapter la sonde dans `eds/run.py`**

Remplacer l'import `from eds.warehouse import (…)` pour y ajouter `source_lake`, et l'import de config pour y ajouter `lecteur_lake`. Puis réécrire `verifier_acces_lake` :

```python
    # Ce que l'exploitant doit faire quand le moteur ne voit pas le lake,
    # selon la façon dont il le lit.
    CORRECTIONS_ACCES_LAKE = {
        "fichier": (
            "Le montage est probablement rompu (le répertoire lake/ a-t-il été "
            "supprimé ?). Correction : docker compose restart clickhouse"
        ),
        "blob": (
            "La named collection eds_lake ne joint pas le conteneur `lake`. "
            "Correction : vérifier lake.xml (chaîne de connexion du compte de "
            "stockage), puis kubectl -n eds logs clickhouse-0"
        ),
    }

    def verifier_acces_lake(self) -> None:
        """Vérifie que ClickHouse voit le lake avant de tenter de le lire.

        Sur le poste, le lake est un montage Docker : supprimer le répertoire
        côté hôte (`rm -rf lake`) le casse, et l'erreur brute du moteur
        (FILE_DOESNT_EXIST) n'oriente pas vers la cause. En cloud, c'est la
        chaîne de connexion de la named collection qui peut être fausse. Un
        témoin écrit par le pipeline puis lu par le moteur tranche dans les
        deux cas, et le message dit quoi faire.
        """
        temoin = LAKE / ".sonde"
        temoin.write_text("sonde", encoding="utf-8")
        table, _ = source_lake(".sonde", "LineAsString")
        try:
            self.ch.command(f"SELECT count() FROM {table}")
        except Exception as erreur:
            raise ErreurPipeline(
                "ClickHouse ne voit pas le lake. "
                + self.CORRECTIONS_ACCES_LAKE[lecteur_lake()]
            ) from erreur
        finally:
            temoin.unlink(missing_ok=True)
```

`CORRECTIONS_ACCES_LAKE` est un attribut de classe de `Pipeline`, placé juste avant la méthode.

- [ ] **Step 5 : vérifier le passage**

Run: `.venv/bin/python -m pytest -q`
Expected: tout vert. Puis, contre l'entrepôt local (Docker démarré) : `.venv/bin/python -m eds.run --jour 2026-08-01` doit se terminer en code 0 et afficher `bronze chargé` pour les sources du jour — preuve que le mode `fichier` est inchangé.

- [ ] **Step 6 : commit**

```bash
git add eds/warehouse.py eds/run.py tests/test_warehouse.py
git commit -m "feat(warehouse): lire le lake par file() ou azureBlobStorage() derrière une seule fonction"
```

---

### Task 3 : `MB_URL` dans `restitution.py`

**Files:**
- Modify: `eds/restitution.py:104` et `:1490`

**Interfaces:**
- Consumes: `config.url_metabase()` (Task 1).

- [ ] **Step 1 : remplacer la constante**

Remplacer :

```python
MB_URL = "http://localhost:3000"
```

par :

```python
# Lu une fois à l'import : `eds.config.url_metabase` charge `.env` puis
# applique `MB_URL` s'il est défini (le nom du Service dans un cluster).
MB_URL = url_metabase()
```

et l'import `from eds.config import exiger, langue_metabase` par `from eds.config import exiger, langue_metabase, url_metabase`. Mettre à jour le commentaire au-dessus (lignes 99-103) : « Exposé par docker-compose (`ports: 3000:3000`) quand ce script tourne sur l'hôte ; `MB_URL=http://metabase:3000` quand il tourne dans le cluster, à côté de Metabase. Le nom `clickhouse` des connexions posées plus bas reste, lui, celui que Metabase doit joindre depuis SON conteneur. »

- [ ] **Step 2 : vérifier**

Run: `.venv/bin/python -m pytest -q` puis `.venv/bin/python -c "import eds.restitution as r; print(r.MB_URL)"`
Expected: tests verts ; affiche `http://localhost:3000`. Puis `MB_URL=http://metabase:3000/ .venv/bin/python -c "import eds.restitution as r; print(r.MB_URL)"` affiche `http://metabase:3000`.

- [ ] **Step 3 : commit**

```bash
git add eds/restitution.py
git commit -m "feat(restitution): adresse de Metabase surchargeable par MB_URL"
```

---

### Task 4 : image du pipeline

**Files:**
- Create: `infra/Dockerfile`
- Create: `.dockerignore`

**Interfaces:**
- Produces: image dont le point d'entrée par défaut est `python -m eds.run --etat`, répertoire de travail `/app`, utilisateur `eds`, `logs/` inscriptible.

- [ ] **Step 1 : écrire `.dockerignore`**

```
# Le contexte envoyé à `az acr build` ne contient que le code : ni données
# (identités en clair dans eds-chu-sujet, lake pseudonymisé), ni journaux,
# ni environnement virtuel, ni historique git.
.git
.venv
.env
.env.example
eds-chu-sujet
lake
logs
docker
docs
exploration
tests
infra/terraform
infra/k8s
Screenshots
**/__pycache__
.pytest_cache
.ruff_cache
```

- [ ] **Step 2 : écrire `infra/Dockerfile`**

```dockerfile
# Image du pipeline EDS — la même pour le chargement, la restitution et le
# CronJob nocturne. Elle ne contient QUE le code : les données arrivent par
# des montages, les secrets par l'environnement.
FROM python:3.14.7-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Les dépendances d'exécution seulement (requirements-dev.txt reste sur le
# poste) : deux paquets, cf. le rapport.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY eds/ eds/
COPY sql/ sql/

# `eds.journal` écrit logs/pipeline.log sous la racine du dépôt (/app) : le
# répertoire doit exister et appartenir à l'utilisateur non root.
RUN useradd --system --create-home --shell /usr/sbin/nologin eds \
    && mkdir -p logs \
    && chown -R eds:eds /app
USER eds

CMD ["python", "-m", "eds.run", "--etat"]
```

- [ ] **Step 3 : construire et vérifier localement**

Run:

```bash
docker build --platform linux/amd64 -f infra/Dockerfile -t eds-pipeline:local . \
  && docker run --rm --platform linux/amd64 eds-pipeline:local python -c "import eds.run, eds.restitution, eds.supervision; print('ok')" \
  && docker run --rm --platform linux/amd64 eds-pipeline:local id -u \
  && docker run --rm --platform linux/amd64 eds-pipeline:local sh -c 'touch logs/x && echo logs inscriptible' \
  && docker run --rm --platform linux/amd64 eds-pipeline:local sh -c 'ls eds-chu-sujet lake 2>&1 | head -1'
```

Expected: `ok` ; un uid non nul ; `logs inscriptible` ; `ls: cannot access 'eds-chu-sujet': No such file or directory` (aucune donnée embarquée).

- [ ] **Step 4 : commit**

```bash
git add infra/Dockerfile .dockerignore
git commit -m "feat(infra): image du pipeline, code seul, utilisateur non root"
```

---

### Task 5 : socle Azure en Terraform

**Files:**
- Create: `infra/terraform/versions.tf`, `variables.tf`, `main.tf`, `storage.tf`, `keyvault.tf`, `acr.tf`, `aks.tf`, `outputs.tf`, `lake.xml.tftpl`, `terraform.tfvars.example`
- Modify: `.gitignore`

**Interfaces:**
- Produces: sorties Terraform `resource_group_name`, `aks_name`, `acr_name`, `acr_login_server`, `key_vault_name`, `storage_account_name`, `tenant_id`, `csi_client_id`, `kubelet_client_id`, `ip_autorisee`. Secrets Key Vault : `ch-admin-password`, `ch-pilotage-password`, `ch-recherche-password`, `ch-exploitation-password`, `eds-pseudo-salt`, `mb-admin-password`, `mb-pilotage-password`, `mb-recherche-password`, `clickhouse-lake-xml`.

- [ ] **Step 1 : `.gitignore`**

Ajouter à la fin :

```
# ── Déploiement cloud ─────────────────────────────────────────────────
# L'état Terraform contient les valeurs des secrets générés ; les tfvars
# portent l'abonnement et l'IP du poste ; les manifestes rendus portent les
# identifiants du déploiement. Aucun des trois n'a sa place dans git.
infra/terraform/.terraform/
infra/terraform/*.tfstate
infra/terraform/*.tfstate.*
infra/terraform/terraform.tfvars
infra/terraform/*.auto.tfvars
infra/k8s/rendu/
```

- [ ] **Step 2 : `versions.tf`**

```hcl
terraform {
  required_version = ">= 1.10"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "5.4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "3.9.0"
    }
    time = {
      source  = "hashicorp/time"
      version = "0.14.1"
    }
  }
}

provider "azurerm" {
  subscription_id = var.subscription_id

  features {
    key_vault {
      # Démonstration jetable : un `destroy` doit vraiment libérer le nom.
      purge_soft_delete_on_destroy = true
    }
    resource_group {
      prevent_deletion_if_contains_resources = false
    }
  }
}
```

- [ ] **Step 3 : `variables.tf`**

```hcl
variable "subscription_id" {
  description = "Abonnement Azure cible (Azure for Students)."
  type        = string
}

variable "prefixe" {
  description = "Préfixe des noms de ressources."
  type        = string
  default     = "eds"
}

variable "region" {
  description = "Région Azure. France Central est certifiée HDS."
  type        = string
  default     = "francecentral"
}

variable "ip_autorisee" {
  description = "Seule plage autorisée à joindre Metabase (CIDR, ex. 203.0.113.4/32)."
  type        = string

  validation {
    condition     = can(cidrnetmask(var.ip_autorisee))
    error_message = "ip_autorisee doit être un CIDR IPv4, par exemple 203.0.113.4/32."
  }
}

variable "taille_noeud" {
  description = "Taille du nœud AKS. B2ms : 2 vCPU, 8 Go, dans le quota étudiant."
  type        = string
  default     = "Standard_B2ms"
}

variable "version_aks" {
  description = "Version mineure de Kubernetes ; le patch suit le canal `patch`."
  type        = string
  default     = "1.35"
}
```

- [ ] **Step 4 : `main.tf`**

```hcl
data "azurerm_client_config" "actuel" {}

# Suffixe aléatoire : Storage Account, Key Vault et ACR ont des noms
# mondialement uniques.
resource "random_string" "suffixe" {
  length  = 6
  lower   = true
  upper   = false
  numeric = true
  special = false
}

locals {
  etiquettes = {
    projet = "eds-chu"
    usage  = "demonstration"
  }
}

resource "azurerm_resource_group" "eds" {
  name     = "rg-${var.prefixe}-cloud"
  location = var.region
  tags     = local.etiquettes
}
```

- [ ] **Step 5 : `storage.tf`**

```hcl
# Un compte, deux conteneurs : `source` reçoit le dépôt du CHU (identités
# en clair — données synthétiques ici, mais le principe HDS vaut), `lake` la
# copie pseudonymisée et projetée que ClickHouse lit.
resource "azurerm_storage_account" "eds" {
  name                            = "sa${var.prefixe}${random_string.suffixe.result}"
  resource_group_name             = azurerm_resource_group.eds.name
  location                        = azurerm_resource_group.eds.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = true
  allow_nested_items_to_be_public = false
  # La clé partagée sert à la named collection ClickHouse ; blobfuse et
  # l'upload passent, eux, par l'identité (RBAC).
  shared_access_key_enabled       = true
  tags                            = local.etiquettes
}

resource "azurerm_storage_container" "source" {
  name                  = "source"
  storage_account_id    = azurerm_storage_account.eds.id
  container_access_type = "private"
}

resource "azurerm_storage_container" "lake" {
  name                  = "lake"
  storage_account_id    = azurerm_storage_account.eds.id
  container_access_type = "private"
}

# L'opérateur envoie le dépôt source avec `az storage blob upload-batch
# --auth-mode login` : il lui faut le rôle données, pas seulement Owner.
resource "azurerm_role_assignment" "operateur_blob" {
  scope                = azurerm_storage_account.eds.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = data.azurerm_client_config.actuel.object_id
}

# blobfuse monte `source` et `lake` dans le pod du pipeline avec l'identité
# du kubelet : aucune clé dans le cluster pour ce chemin-là.
resource "azurerm_role_assignment" "kubelet_blob" {
  scope                = azurerm_storage_account.eds.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_kubernetes_cluster.eds.kubelet_identity[0].object_id
}
```

- [ ] **Step 6 : `keyvault.tf` et `lake.xml.tftpl`**

`lake.xml.tftpl` :

```xml
<clickhouse>
  <named_collections>
    <eds_lake>
      <connection_string>DefaultEndpointsProtocol=https;AccountName=${compte};AccountKey=${cle};EndpointSuffix=core.windows.net</connection_string>
      <container>${conteneur}</container>
      <blob_path>*</blob_path>
    </eds_lake>
  </named_collections>
</clickhouse>
```

`keyvault.tf` :

```hcl
resource "azurerm_key_vault" "eds" {
  name                       = "kv-${var.prefixe}-${random_string.suffixe.result}"
  resource_group_name        = azurerm_resource_group.eds.name
  location                   = azurerm_resource_group.eds.location
  tenant_id                  = data.azurerm_client_config.actuel.tenant_id
  sku_name                   = "standard"
  rbac_authorization_enabled = true
  # Jetable : sans protection contre la purge, sinon le nom reste bloqué
  # 90 jours après `destroy`.
  purge_protection_enabled   = false
  soft_delete_retention_days = 7
  tags                       = local.etiquettes
}

# Terraform écrit les secrets avec l'identité de l'opérateur.
resource "azurerm_role_assignment" "operateur_secrets" {
  scope                = azurerm_key_vault.eds.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = data.azurerm_client_config.actuel.object_id
}

# L'addon CSI de l'AKS lit les secrets pour les projeter dans les pods.
resource "azurerm_role_assignment" "csi_secrets" {
  scope                = azurerm_key_vault.eds.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_kubernetes_cluster.eds.key_vault_secrets_provider[0].secret_identity[0].object_id
}

# Un rôle RBAC met jusqu'à une minute à devenir effectif sur le plan de
# données : écrire un secret avant serait refusé (403) de façon aléatoire.
resource "time_sleep" "propagation_rbac" {
  depends_on      = [azurerm_role_assignment.operateur_secrets]
  create_duration = "90s"
}

# ── Secrets générés ─────────────────────────────────────────────────────
# Mots de passe ClickHouse et sel : alphanumériques, pour rester valides
# dans une variable d'environnement, un fichier XML et un GRANT SQL.
resource "random_password" "clickhouse" {
  for_each = toset(["ch-admin-password", "ch-pilotage-password", "ch-recherche-password", "ch-exploitation-password"])
  length   = 32
  special  = false
}

resource "random_password" "sel" {
  length  = 64
  special = false
}

# Metabase exige (complexité « normal ») au moins un chiffre, une minuscule,
# une majuscule et un caractère spécial ; on restreint les spéciaux à ceux
# qui ne posent aucun problème dans un JSON ou un shell.
resource "random_password" "metabase" {
  for_each         = toset(["mb-admin-password", "mb-pilotage-password", "mb-recherche-password"])
  length           = 24
  special          = true
  override_special = "!@#%^*_-+=."
  min_lower        = 1
  min_upper        = 1
  min_numeric      = 1
  min_special      = 1
}

resource "azurerm_key_vault_secret" "clickhouse" {
  for_each     = random_password.clickhouse
  name         = each.key
  value        = each.value.result
  key_vault_id = azurerm_key_vault.eds.id
  depends_on   = [time_sleep.propagation_rbac]
}

resource "azurerm_key_vault_secret" "sel" {
  name         = "eds-pseudo-salt"
  value        = random_password.sel.result
  key_vault_id = azurerm_key_vault.eds.id
  depends_on   = [time_sleep.propagation_rbac]
}

resource "azurerm_key_vault_secret" "metabase" {
  for_each     = random_password.metabase
  name         = each.key
  value        = each.value.result
  key_vault_id = azurerm_key_vault.eds.id
  depends_on   = [time_sleep.propagation_rbac]
}

# La named collection entière est un secret : elle contient la clé du compte.
resource "azurerm_key_vault_secret" "lake_xml" {
  name = "clickhouse-lake-xml"
  value = templatefile("${path.module}/lake.xml.tftpl", {
    compte    = azurerm_storage_account.eds.name
    cle       = azurerm_storage_account.eds.primary_access_key
    conteneur = azurerm_storage_container.lake.name
  })
  key_vault_id = azurerm_key_vault.eds.id
  depends_on   = [time_sleep.propagation_rbac]
}
```

- [ ] **Step 7 : `acr.tf`**

```hcl
resource "azurerm_container_registry" "eds" {
  name                = "acr${var.prefixe}${random_string.suffixe.result}"
  resource_group_name = azurerm_resource_group.eds.name
  location            = azurerm_resource_group.eds.location
  sku                 = "Basic"
  admin_enabled       = false
  tags                = local.etiquettes
}

resource "azurerm_role_assignment" "kubelet_acr" {
  scope                = azurerm_container_registry.eds.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_kubernetes_cluster.eds.kubelet_identity[0].object_id
}
```

- [ ] **Step 8 : `aks.tf`**

```hcl
resource "azurerm_kubernetes_cluster" "eds" {
  name                      = "aks-${var.prefixe}"
  resource_group_name       = azurerm_resource_group.eds.name
  location                  = azurerm_resource_group.eds.location
  dns_prefix                = "aks-${var.prefixe}"
  kubernetes_version        = var.version_aks
  automatic_upgrade_channel = "patch"
  sku_tier                  = "Free"
  tags                      = local.etiquettes

  default_node_pool {
    name            = "system"
    node_count      = 1
    vm_size         = var.taille_noeud
    os_disk_size_gb = 64
    os_sku          = "AzureLinux"
  }

  identity {
    type = "SystemAssigned"
  }

  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
  }

  # Projette les secrets Key Vault dans les pods (SecretProviderClass).
  key_vault_secrets_provider {
    secret_rotation_enabled = true
  }

  # Monte les conteneurs Blob dans les pods (blobfuse).
  storage_profile {
    blob_driver_enabled = true
  }
}
```

- [ ] **Step 9 : `outputs.tf`**

```hcl
output "resource_group_name" {
  value = azurerm_resource_group.eds.name
}

output "aks_name" {
  value = azurerm_kubernetes_cluster.eds.name
}

output "acr_name" {
  value = azurerm_container_registry.eds.name
}

output "acr_login_server" {
  value = azurerm_container_registry.eds.login_server
}

output "key_vault_name" {
  value = azurerm_key_vault.eds.name
}

output "storage_account_name" {
  value = azurerm_storage_account.eds.name
}

output "tenant_id" {
  value = data.azurerm_client_config.actuel.tenant_id
}

output "csi_client_id" {
  description = "Identité de l'addon Key Vault CSI, à donner à la SecretProviderClass."
  value       = azurerm_kubernetes_cluster.eds.key_vault_secrets_provider[0].secret_identity[0].client_id
}

output "kubelet_client_id" {
  description = "Identité du kubelet, avec laquelle blobfuse s'authentifie."
  value       = azurerm_kubernetes_cluster.eds.kubelet_identity[0].client_id
}

output "ip_autorisee" {
  value = var.ip_autorisee
}
```

- [ ] **Step 10 : `terraform.tfvars.example`**

```hcl
# Copier en terraform.tfvars (non versionné) puis renseigner.
subscription_id = "00000000-0000-0000-0000-000000000000"
# Votre adresse publique : curl -s https://api.ipify.org
ip_autorisee = "203.0.113.4/32"
```

- [ ] **Step 11 : valider**

Run :

```bash
cd infra/terraform && terraform init -input=false && terraform fmt -check -recursive && terraform validate
```

Expected : `Success! The configuration is valid.` Si `validate` refuse un attribut, consulter `terraform providers schema -json | python3 -m json.tool | grep -n <attribut>` et corriger le nom : les noms de ce plan ont été relus dans la documentation azurerm 5.4.0, mais un bloc peut différer.

Puis, avec un `terraform.tfvars` réel (abonnement `az account show --query id -o tsv`, IP `curl -s https://api.ipify.org`) :

```bash
terraform plan -input=false -out=plan.tfplan | tail -5
```

Expected : `Plan: N to add, 0 to change, 0 to destroy.` sans erreur. Supprimer `plan.tfplan` ensuite. **Ne pas appliquer ici** : l'apply est la Task 9.

- [ ] **Step 12 : commit**

```bash
git add .gitignore infra/terraform
git commit -m "feat(infra): socle Azure en Terraform — stockage, coffre, registre, AKS"
```

Vérifier avant : `git status --short infra/terraform` ne montre ni `.tfstate`, ni `terraform.tfvars`, ni `.terraform/`. Le fichier `.terraform.lock.hcl` est bien commité.

---

### Task 6 : manifestes Kubernetes

**Files:**
- Create: `infra/k8s/base/kustomization.yaml`, `namespace.yaml`, `secrets.yaml`, `config.yaml`, `storage.yaml`, `clickhouse.yaml`, `metabase.yaml`, `cronjob.yaml`, `job-charger.yaml`, `job-restituer.yaml`

**Interfaces:**
- Consumes: secrets Key Vault de la Task 5 ; image de la Task 4 ; variables Python des Tasks 1-3.
- Produces: marque-places remplacés par `ops/cloud.sh rendre` (Task 7) : `__ACR_LOGIN_SERVER__`, `__TAG__`, `__KEY_VAULT__`, `__TENANT_ID__`, `__CSI_CLIENT_ID__`, `__STORAGE_ACCOUNT__`, `__RESOURCE_GROUP__`, `__KUBELET_CLIENT_ID__`, `__IP_AUTORISEE__`. Secret k8s `eds-secrets`, ConfigMap `eds-config`, Services `clickhouse` et `metabase`, PVC `source` et `lake`.

- [ ] **Step 1 : `kustomization.yaml` et `namespace.yaml`**

```yaml
# kustomization.yaml — ce que `kubectl apply -k` pose en une fois. Les Jobs
# ne sont pas listés : immuables, ils sont recréés à chaque exécution par
# ops/cloud.sh.
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - namespace.yaml
  - secrets.yaml
  - config.yaml
  - storage.yaml
  - clickhouse.yaml
  - metabase.yaml
  - cronjob.yaml
```

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: eds
```

- [ ] **Step 2 : `secrets.yaml`**

```yaml
# Les secrets ne sont jamais écrits dans un manifeste : l'addon CSI les lit
# dans Key Vault avec son identité managée, les monte dans chaque pod sous
# /mnt/secrets, et les recopie dans le Secret `eds-secrets` que les pods
# lisent en variables d'environnement. Le Secret n'existe que tant qu'un pod
# monte ce volume : tous le montent.
apiVersion: secrets-store.csi.x-k8s.io/v1
kind: SecretProviderClass
metadata:
  name: eds-keyvault
  namespace: eds
spec:
  provider: azure
  parameters:
    usePodIdentity: "false"
    useVMManagedIdentity: "true"
    userAssignedIdentityID: "__CSI_CLIENT_ID__"
    keyvaultName: "__KEY_VAULT__"
    tenantId: "__TENANT_ID__"
    objects: |
      array:
        - |
          objectName: ch-admin-password
          objectType: secret
        - |
          objectName: ch-pilotage-password
          objectType: secret
        - |
          objectName: ch-recherche-password
          objectType: secret
        - |
          objectName: ch-exploitation-password
          objectType: secret
        - |
          objectName: eds-pseudo-salt
          objectType: secret
        - |
          objectName: mb-admin-password
          objectType: secret
        - |
          objectName: mb-pilotage-password
          objectType: secret
        - |
          objectName: mb-recherche-password
          objectType: secret
        - |
          objectName: clickhouse-lake-xml
          objectType: secret
          objectAlias: lake.xml
  secretObjects:
    - secretName: eds-secrets
      type: Opaque
      data:
        - objectName: ch-admin-password
          key: CH_ADMIN_PASSWORD
        - objectName: ch-pilotage-password
          key: CH_PILOTAGE_PASSWORD
        - objectName: ch-recherche-password
          key: CH_RECHERCHE_PASSWORD
        - objectName: ch-exploitation-password
          key: CH_EXPLOITATION_PASSWORD
        - objectName: eds-pseudo-salt
          key: EDS_PSEUDO_SALT
        - objectName: mb-admin-password
          key: MB_ADMIN_PASSWORD
        - objectName: mb-pilotage-password
          key: MB_PILOTAGE_PASSWORD
        - objectName: mb-recherche-password
          key: MB_RECHERCHE_PASSWORD
```

- [ ] **Step 3 : `config.yaml`**

```yaml
# Tout ce qui n'est pas secret et que le local lit dans `.env`.
apiVersion: v1
kind: ConfigMap
metadata:
  name: eds-config
  namespace: eds
data:
  CH_ADMIN_USER: eds_admin
  CH_HOST: clickhouse
  CH_PORT: "8123"
  EDS_LAKE_LECTEUR: blob
  EDS_SOURCE: /data/source
  EDS_LAKE: /data/lake
  MB_URL: http://metabase:3000
  MB_LOCALE: fr
  MB_ADMIN_EMAIL: admin@eds-chu.local
  MB_PILOTAGE_EMAIL: pilotage@eds-chu.local
  MB_RECHERCHE_EMAIL: recherche@eds-chu.local
```

- [ ] **Step 4 : `storage.yaml`**

```yaml
# Les deux conteneurs Blob, vus comme des répertoires par le pipeline. Le
# montage s'authentifie avec l'identité du kubelet (rôle « Storage Blob Data
# Contributor » posé par Terraform) : pas de clé dans le cluster. `source`
# est monté en lecture seule, comme sur le poste : le pipeline n'y écrit
# jamais.
apiVersion: v1
kind: PersistentVolume
metadata:
  name: eds-source
spec:
  capacity:
    storage: 1Gi
  accessModes: [ReadOnlyMany]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: blob-fuse
  mountOptions:
    - -o ro
    - -o allow_other
    - --file-cache-timeout-in-seconds=120
  csi:
    driver: blob.csi.azure.com
    volumeHandle: "__RESOURCE_GROUP__#__STORAGE_ACCOUNT__#source"
    volumeAttributes:
      protocol: fuse2
      resourceGroup: "__RESOURCE_GROUP__"
      storageAccount: "__STORAGE_ACCOUNT__"
      containerName: source
      AzureStorageAuthType: MSI
      AzureStorageIdentityClientID: "__KUBELET_CLIENT_ID__"
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: source
  namespace: eds
spec:
  accessModes: [ReadOnlyMany]
  storageClassName: blob-fuse
  volumeName: eds-source
  resources:
    requests:
      storage: 1Gi
---
apiVersion: v1
kind: PersistentVolume
metadata:
  name: eds-lake
spec:
  capacity:
    storage: 1Gi
  accessModes: [ReadWriteMany]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: blob-fuse
  mountOptions:
    - -o allow_other
    # Pas de cache de lecture : ClickHouse lit le blob par l'API juste après
    # que le pipeline l'a écrit, la vue du pod doit être celle du conteneur.
    - --file-cache-timeout-in-seconds=0
  csi:
    driver: blob.csi.azure.com
    volumeHandle: "__RESOURCE_GROUP__#__STORAGE_ACCOUNT__#lake"
    volumeAttributes:
      protocol: fuse2
      resourceGroup: "__RESOURCE_GROUP__"
      storageAccount: "__STORAGE_ACCOUNT__"
      containerName: lake
      AzureStorageAuthType: MSI
      AzureStorageIdentityClientID: "__KUBELET_CLIENT_ID__"
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: lake
  namespace: eds
spec:
  accessModes: [ReadWriteMany]
  storageClassName: blob-fuse
  volumeName: eds-lake
  resources:
    requests:
      storage: 1Gi
```

- [ ] **Step 5 : `clickhouse.yaml`**

```yaml
# Le moteur. Jamais exposé hors du cluster : seul le Service ClusterIP le
# joint, donc Metabase et le pipeline. Le cloisonnement pilotage/recherche
# reste prononcé par lui (sql/50_droits.sql), pas par le réseau.
apiVersion: v1
kind: Service
metadata:
  name: clickhouse
  namespace: eds
spec:
  type: ClusterIP
  selector:
    app: clickhouse
  ports:
    - name: http
      port: 8123
    - name: natif
      port: 9000
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: clickhouse
  namespace: eds
spec:
  serviceName: clickhouse
  replicas: 1
  selector:
    matchLabels:
      app: clickhouse
  template:
    metadata:
      labels:
        app: clickhouse
    spec:
      containers:
        - name: clickhouse
          # 25.8 : dernière ligne LTS compatible avec Metabase (la 26.x casse
          # l'affichage des refus de droits). Patch épinglé.
          image: clickhouse/clickhouse-server:25.8.33.6
          ports:
            - containerPort: 8123
            - containerPort: 9000
          env:
            - name: CLICKHOUSE_USER
              valueFrom:
                configMapKeyRef: { name: eds-config, key: CH_ADMIN_USER }
            - name: CLICKHOUSE_PASSWORD
              valueFrom:
                secretKeyRef: { name: eds-secrets, key: CH_ADMIN_PASSWORD }
            # CREATE USER / GRANT en SQL : le cloisonnement vit dans des
            # fichiers .sql versionnés, pas dans du XML manuel.
            - name: CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT
              value: "1"
          resources:
            requests: { cpu: 500m, memory: 2Gi }
            limits: { memory: 3Gi }
          volumeMounts:
            - name: donnees
              mountPath: /var/lib/clickhouse
            # La named collection `eds_lake`, rendue par Terraform avec la
            # chaîne de connexion. `subPath` pour ne pas masquer le reste de
            # config.d (dont docker_related_config.xml, qui ouvre l'écoute).
            - name: secrets
              mountPath: /etc/clickhouse-server/config.d/lake.xml
              subPath: lake.xml
              readOnly: true
            # Le même volume, entier : sert aux démonstrations de refus
            # (`kubectl exec … clickhouse-client --user eds_pilotage`), qui
            # lisent le mot de passe dans le fichier plutôt que de l'afficher.
            - name: secrets
              mountPath: /mnt/secrets
              readOnly: true
          readinessProbe:
            exec:
              command:
                - sh
                - -c
                - clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" --query "SELECT 1"
            initialDelaySeconds: 10
            periodSeconds: 5
            failureThreshold: 20
          livenessProbe:
            httpGet:
              path: /ping
              port: 8123
            initialDelaySeconds: 60
            periodSeconds: 15
      volumes:
        - name: secrets
          csi:
            driver: secrets-store.csi.k8s.io
            readOnly: true
            volumeAttributes:
              secretProviderClass: eds-keyvault
  volumeClaimTemplates:
    - metadata:
        name: donnees
      spec:
        accessModes: [ReadWriteOnce]
        storageClassName: managed-csi
        resources:
          requests:
            storage: 10Gi
```

- [ ] **Step 6 : `metabase.yaml`**

```yaml
# La restitution. Seul composant exposé, et seulement à l'IP de l'opérateur.
# Base applicative H2 sur un disque : acceptable pour une démonstration,
# `eds.restitution` reconstruit de toute façon tout ce qu'elle contient.
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: metabase-data
  namespace: eds
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: managed-csi
  resources:
    requests:
      storage: 2Gi
---
apiVersion: v1
kind: Service
metadata:
  name: metabase
  namespace: eds
spec:
  type: LoadBalancer
  loadBalancerSourceRanges:
    - "__IP_AUTORISEE__"
  selector:
    app: metabase
  ports:
    - port: 3000
      targetPort: 3000
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: metabase
  namespace: eds
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: metabase
  template:
    metadata:
      labels:
        app: metabase
    spec:
      containers:
        - name: metabase
          # Ligne LTS, patch épinglé — même règle que docker-compose.yml.
          image: metabase/metabase:v0.58.32.1
          ports:
            - containerPort: 3000
          env:
            - name: MB_DB_TYPE
              value: h2
            - name: MB_DB_FILE
              value: /metabase-data/metabase.db
            - name: MB_LOAD_SAMPLE_CONTENT
              value: "false"
            - name: JAVA_TIMEZONE
              value: Europe/Paris
            - name: JAVA_OPTS
              value: -Xmx1g
          resources:
            requests: { cpu: 250m, memory: 1Gi }
            limits: { memory: 2Gi }
          volumeMounts:
            - name: donnees
              mountPath: /metabase-data
            - name: secrets
              mountPath: /mnt/secrets
              readOnly: true
          readinessProbe:
            httpGet:
              path: /api/health
              port: 3000
            initialDelaySeconds: 60
            periodSeconds: 10
            failureThreshold: 30
      volumes:
        - name: donnees
          persistentVolumeClaim:
            claimName: metabase-data
        - name: secrets
          csi:
            driver: secrets-store.csi.k8s.io
            readOnly: true
            volumeAttributes:
              secretProviderClass: eds-keyvault
```

- [ ] **Step 7 : `job-charger.yaml`, `job-restituer.yaml`, `cronjob.yaml`**

Le gabarit de pod est le même dans les trois ; il est répété en entier, un lecteur d'un seul fichier doit le comprendre.

`job-charger.yaml` :

```yaml
# Chargement initial : relit tout le dépôt. Recréé à chaque `ops/cloud.sh
# charger`, un Job étant immuable.
apiVersion: batch/v1
kind: Job
metadata:
  name: eds-charger
  namespace: eds
spec:
  backoffLimit: 3
  template:
    metadata:
      labels:
        app: eds-pipeline
    spec:
      restartPolicy: Never
      containers:
        - name: pipeline
          image: __ACR_LOGIN_SERVER__/eds-pipeline:__TAG__
          command: ["python", "-m", "eds.run", "--tout"]
          envFrom:
            - configMapRef: { name: eds-config }
            - secretRef: { name: eds-secrets }
          resources:
            requests: { cpu: 250m, memory: 512Mi }
            limits: { memory: 1Gi }
          volumeMounts:
            - name: source
              mountPath: /data/source
              readOnly: true
            - name: lake
              mountPath: /data/lake
            - name: secrets
              mountPath: /mnt/secrets
              readOnly: true
      volumes:
        - name: source
          persistentVolumeClaim:
            claimName: source
            readOnly: true
        - name: lake
          persistentVolumeClaim:
            claimName: lake
        - name: secrets
          csi:
            driver: secrets-store.csi.k8s.io
            readOnly: true
            volumeAttributes:
              secretProviderClass: eds-keyvault
```

`job-restituer.yaml` : identique, avec `name: eds-restituer` et `command: ["python", "-m", "eds.restitution"]`, sans les volumes `source` et `lake` (la restitution ne touche pas au lake), en gardant `secrets`.

`cronjob.yaml` :

```yaml
# La nuit du CHU. Ce que `eds.supervision` ajoute à `cron` sur le poste —
# verrou, relance bornée — est ici natif : `concurrencyPolicy: Forbid`
# empêche deux exécutions de se recouvrir, `backoffLimit` relance un échec.
# L'alerte est l'état du Job (`kubectl -n eds get jobs`).
apiVersion: batch/v1
kind: CronJob
metadata:
  name: eds-nuit
  namespace: eds
spec:
  schedule: "10 3 * * *"
  timeZone: Europe/Paris
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      backoffLimit: 3
      template:
        metadata:
          labels:
            app: eds-pipeline
        spec:
          restartPolicy: Never
          containers:
            - name: pipeline
              image: __ACR_LOGIN_SERVER__/eds-pipeline:__TAG__
              command: ["python", "-m", "eds.run"]
              envFrom:
                - configMapRef: { name: eds-config }
                - secretRef: { name: eds-secrets }
              resources:
                requests: { cpu: 250m, memory: 512Mi }
                limits: { memory: 1Gi }
              volumeMounts:
                - name: source
                  mountPath: /data/source
                  readOnly: true
                - name: lake
                  mountPath: /data/lake
                - name: secrets
                  mountPath: /mnt/secrets
                  readOnly: true
          volumes:
            - name: source
              persistentVolumeClaim:
                claimName: source
                readOnly: true
            - name: lake
              persistentVolumeClaim:
                claimName: lake
            - name: secrets
              csi:
                driver: secrets-store.csi.k8s.io
                readOnly: true
                volumeAttributes:
                  secretProviderClass: eds-keyvault
```

- [ ] **Step 8 : valider la syntaxe hors ligne**

Run :

```bash
kubectl kustomize infra/k8s/base > /dev/null && echo kustomize ok
for f in infra/k8s/base/job-*.yaml; do kubectl apply --dry-run=client --validate=false -f "$f" -o name; done
grep -rho "__[A-Z_]*__" infra/k8s/base | sort -u
```

Expected : `kustomize ok` ; deux lignes `job.batch/…` ; la liste des marque-places est exactement `__ACR_LOGIN_SERVER__ __CSI_CLIENT_ID__ __IP_AUTORISEE__ __KEY_VAULT__ __KUBELET_CLIENT_ID__ __RESOURCE_GROUP__ __STORAGE_ACCOUNT__ __TAG__ __TENANT_ID__`. Sans contexte kubectl, `--dry-run=client` peut refuser de résoudre l'API : dans ce cas `kubectl kustomize` sur un kustomization temporaire listant les deux Jobs remplit le même office.

- [ ] **Step 9 : commit**

```bash
git add infra/k8s
git commit -m "feat(infra): manifestes Kubernetes — ClickHouse, Metabase, pipeline, secrets par Key Vault"
```

---

### Task 7 : `ops/cloud.sh`

**Files:**
- Create: `ops/cloud.sh` (exécutable)

**Interfaces:**
- Consumes: sorties Terraform (Task 5), marque-places (Task 6), image (Task 4).
- Produces: `ops/cloud.sh deployer|charger|restituer|etat|detruire [--oui]`. Rend `infra/k8s/base/*.yaml` en `infra/k8s/rendu/`.

- [ ] **Step 1 : écrire le script**

```bash
#!/usr/bin/env bash
# Exploitation du déploiement cloud — l'équivalent de « docker compose up »
# pour Azure. Quatre verbes, tous rejouables :
#
#   ops/cloud.sh deployer    crée l'infrastructure, construit l'image, envoie
#                            le dépôt source, pose les manifestes
#   ops/cloud.sh charger     recharge tout le dépôt (Job eds-charger)
#   ops/cloud.sh restituer   provisionne Metabase (Job eds-restituer) et
#                            affiche l'adresse et le compte administrateur
#   ops/cloud.sh etat        ce qui tourne
#   ops/cloud.sh detruire    supprime tout ; --oui saute la confirmation
#
# Prérequis : az (connecté), terraform, kubectl, et
# infra/terraform/terraform.tfvars renseigné (cf. terraform.tfvars.example).
set -euo pipefail

RACINE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF="$RACINE/infra/terraform"
BASE="$RACINE/infra/k8s/base"
RENDU="$RACINE/infra/k8s/rendu"
NS=eds
DELAI_JOB_S=1200

echo_err() { echo "$*" >&2; }

verifier_outils() {
  for outil in az terraform kubectl git; do
    command -v "$outil" >/dev/null || { echo_err "outil manquant : $outil"; exit 2; }
  done
  az account show >/dev/null 2>&1 || { echo_err "az login requis"; exit 2; }
  [[ -f "$TF/terraform.tfvars" ]] || {
    echo_err "infra/terraform/terraform.tfvars absent : copier terraform.tfvars.example"; exit 2; }
}

sortie_tf() { terraform -chdir="$TF" output -raw "$1"; }

# Remplace les marque-places des manifestes par les sorties Terraform. Le
# résultat porte des identifiants propres à CE déploiement : il n'est pas
# versionné (cf. .gitignore).
rendre() {
  local tag="$1"
  local acr kv tenant csi sa rg kubelet ip
  acr=$(sortie_tf acr_login_server); kv=$(sortie_tf key_vault_name)
  tenant=$(sortie_tf tenant_id);     csi=$(sortie_tf csi_client_id)
  sa=$(sortie_tf storage_account_name); rg=$(sortie_tf resource_group_name)
  kubelet=$(sortie_tf kubelet_client_id); ip=$(sortie_tf ip_autorisee)
  rm -rf "$RENDU"; mkdir -p "$RENDU"
  for f in "$BASE"/*.yaml; do
    sed -e "s|__ACR_LOGIN_SERVER__|$acr|g" -e "s|__TAG__|$tag|g" \
        -e "s|__KEY_VAULT__|$kv|g"        -e "s|__TENANT_ID__|$tenant|g" \
        -e "s|__CSI_CLIENT_ID__|$csi|g"   -e "s|__STORAGE_ACCOUNT__|$sa|g" \
        -e "s|__RESOURCE_GROUP__|$rg|g"   -e "s|__KUBELET_CLIENT_ID__|$kubelet|g" \
        -e "s|__IP_AUTORISEE__|$ip|g" "$f" > "$RENDU/$(basename "$f")"
  done
  if grep -rq "__[A-Z_]*__" "$RENDU"; then
    echo_err "marque-place non rendu :"; grep -rho "__[A-Z_]*__" "$RENDU" | sort -u >&2; exit 1
  fi
}

tag_image() { git -C "$RACINE" rev-parse --short HEAD; }

deployer() {
  verifier_outils
  terraform -chdir="$TF" init -input=false
  terraform -chdir="$TF" apply -input=false -auto-approve
  local tag; tag=$(tag_image)
  # Construite dans Azure, pour l'architecture des nœuds : ni Docker local,
  # ni croisement ARM/AMD64 depuis un Mac.
  az acr build --registry "$(sortie_tf acr_name)" --image "eds-pipeline:$tag" \
    --platform linux/amd64 --file "$RACINE/infra/Dockerfile" "$RACINE"
  az aks get-credentials --resource-group "$(sortie_tf resource_group_name)" \
    --name "$(sortie_tf aks_name)" --overwrite-existing
  # Le dépôt du CHU, par l'identité de l'opérateur (rôle posé par Terraform).
  az storage blob upload-batch --auth-mode login --account-name "$(sortie_tf storage_account_name)" \
    --destination source --source "$RACINE/eds-chu-sujet/source-filestorage" --overwrite --only-show-errors
  rendre "$tag"
  kubectl apply -k "$RENDU"
  kubectl -n "$NS" rollout status statefulset/clickhouse --timeout=600s
  echo "déployé — image eds-pipeline:$tag"
}

# Lance un Job à partir de son manifeste rendu, attend sa fin, affiche ses
# journaux, rend son code de sortie.
lancer_job() {
  local nom="$1"
  kubectl -n "$NS" delete job "$nom" --ignore-not-found --wait=true
  kubectl apply -f "$RENDU/job-${nom#eds-}.yaml"
  local debut=$SECONDS
  while true; do
    local reussi echoue
    reussi=$(kubectl -n "$NS" get job "$nom" -o jsonpath='{.status.succeeded}')
    echoue=$(kubectl -n "$NS" get job "$nom" -o jsonpath='{.status.conditions[?(@.type=="Failed")].status}')
    [[ "${reussi:-0}" -ge 1 ]] && break
    if [[ "$echoue" == "True" ]]; then
      kubectl -n "$NS" logs "job/$nom" --all-containers --tail=200 || true
      echo_err "job $nom en échec"; return 1
    fi
    (( SECONDS - debut > DELAI_JOB_S )) && { echo_err "job $nom : délai dépassé"; return 1; }
    sleep 5
  done
  kubectl -n "$NS" logs "job/$nom" --all-containers
}

charger() { verifier_outils; [[ -d "$RENDU" ]] || rendre "$(tag_image)"; lancer_job eds-charger; }

restituer() {
  verifier_outils; [[ -d "$RENDU" ]] || rendre "$(tag_image)"
  lancer_job eds-restituer
  local ip
  ip=$(kubectl -n "$NS" get svc metabase -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
  echo
  echo "Metabase          http://$ip:3000  (depuis $(sortie_tf ip_autorisee) seulement)"
  echo "Administrateur    $(kubectl -n "$NS" get configmap eds-config -o jsonpath='{.data.MB_ADMIN_EMAIL}')"
  echo "Mot de passe      az keyvault secret show --vault-name $(sortie_tf key_vault_name) --name mb-admin-password --query value -o tsv"
}

etat() {
  verifier_outils
  kubectl -n "$NS" get pods,jobs,cronjobs,svc,pvc
}

detruire() {
  verifier_outils
  if [[ "${1:-}" != "--oui" ]]; then
    read -r -p "Détruire tout le déploiement cloud (groupe $(sortie_tf resource_group_name)) ? [oui/N] " reponse
    [[ "$reponse" == "oui" ]] || { echo "abandon"; exit 0; }
  fi
  # Les Services LoadBalancer d'abord : Terraform ne connaît pas l'IP
  # publique créée par Kubernetes, elle bloquerait la suppression du groupe.
  if [[ -d "$RENDU" ]] && kubectl get ns "$NS" >/dev/null 2>&1; then
    kubectl delete -k "$RENDU" --ignore-not-found --wait=true --timeout=300s || true
  fi
  terraform -chdir="$TF" destroy -input=false -auto-approve
  rm -rf "$RENDU"
  echo "détruit"
}

case "${1:-}" in
  deployer)  deployer ;;
  charger)   charger ;;
  restituer) restituer ;;
  etat)      etat ;;
  detruire)  detruire "${2:-}" ;;
  *) sed -n '2,15p' "$0"; exit 2 ;;
esac
```

- [ ] **Step 2 : vérifier hors ligne**

Run :

```bash
chmod +x ops/cloud.sh && bash -n ops/cloud.sh && ops/cloud.sh ; echo "code $?"
```

Expected : `bash -n` silencieux ; l'aide s'affiche ; `code 2`. Si `shellcheck` est installé : `shellcheck ops/cloud.sh` sans avertissement bloquant.

- [ ] **Step 3 : commit**

```bash
git add ops/cloud.sh
git commit -m "feat(ops): déployer, charger, restituer et détruire l'entrepôt sur Azure en une commande"
```

---

### Task 8 : documentation

**Files:**
- Modify: `README.md` (nouvelle section « Déploiement cloud » entre « Planification » et « Vérifier », et ligne dans « Organisation du dépôt »)
- Modify: `docs/RAPPORT.md` (nouvelle « Partie 4 — Le déploiement cloud », avant « Validation des chiffres »)

- [ ] **Step 1 : README**

Ajouter après la sous-section « Planification » (avant `## Vérifier`) :

````markdown
## Déploiement cloud

Le même entrepôt tourne sur Azure, pour une démonstration jetable : le
socle est décrit en Terraform (`infra/terraform/`), les composants en
manifestes Kubernetes (`infra/k8s/`), l'image du pipeline en
`infra/Dockerfile`. Rien ne change dans le code : chaque différence avec le
poste est une variable d'environnement (`EDS_SOURCE`, `EDS_LAKE`,
`EDS_LAKE_LECTEUR=blob`, `CH_HOST`, `MB_URL`).

```bash
cp infra/terraform/terraform.tfvars.example infra/terraform/terraform.tfvars   # abonnement, IP
az login
ops/cloud.sh deployer      # ~10 min : AKS, stockage, coffre, registre, image, manifestes
ops/cloud.sh charger       # eds.run --tout dans le cluster
ops/cloud.sh restituer     # eds.restitution, puis l'adresse de Metabase
ops/cloud.sh detruire      # tout, y compris l'IP publique
```

| Sur le poste | Dans le cluster | Pourquoi |
| --- | --- | --- |
| `lake/` monté dans ClickHouse, lu par `file()` | conteneur Blob `lake`, lu par `azureBlobStorage()` via la named collection `eds_lake` | pas de volume partagé entre pods ; la clé du stockage reste dans la configuration du serveur, jamais dans une requête |
| `.env` | Key Vault, projeté par l'addon CSI en Secret `eds-secrets` | aucun secret dans un manifeste ; tous générés par Terraform |
| `cron` + `eds.supervision` | CronJob `eds-nuit`, `concurrencyPolicy: Forbid`, `backoffLimit: 3` | verrou et relance sont natifs à l'orchestrateur |
| `localhost:3000` | Service LoadBalancer restreint à `ip_autorisee` | ClickHouse, lui, n'est jamais exposé |

Coût, cluster allumé : ≈ 2,8 €/jour (nœud B2ms, équilibreur, registre, disques).
Une démonstration de trois heures coûte moins d'un euro ; `detruire` ramène à zéro.
````

Dans « Organisation du dépôt », ajouter les lignes `infra/` (« déploiement cloud : Terraform, Kubernetes, Dockerfile ») et `ops/cloud.sh` à l'endroit où l'arborescence liste `ops/`.

- [ ] **Step 2 : RAPPORT, Partie 4**

Insérer avant `# Validation des chiffres` une partie de 250 à 400 lignes, structurée ainsi (rédiger en prose complète, dans le ton du reste du rapport) :

```markdown
# Partie 4 — Le déploiement cloud

## 18. Ce qui est déployé, et pourquoi là

### 18.1 Le même entrepôt, ailleurs
(le principe : aucune bifurcation dans le code, cinq variables d'environnement ;
tableau poste/cluster repris du README ; schéma ASCII de la spec § 2)

### 18.2 AKS plutôt que Container Apps
(trois workloads seulement ; Container Apps aurait suffi et coûté moins ;
AKS retenu parce que StatefulSet, CronJob et CSI sont ce que le sujet demande
de démontrer ; Free tier, un nœud, quota étudiant de 4 vCPU en famille B)

### 18.3 Blob plutôt qu'un volume partagé
(le pipeline voit un système de fichiers par blobfuse, ClickHouse lit par
l'API ; pas de ReadWriteMany entre deux pods ; la provenance `_source_path`
garde la même forme, prouvée par un test)

## 19. La sécurité, dans l'ordre où elle est prononcée
(HDS et France Central ; identités en clair dans `source`, données
synthétiques ; secrets générés par Terraform, jamais lus par un humain, projetés
par CSI ; blobfuse par identité managée, aucune clé ; la named collection
comme seul endroit où la clé existe, dans un fichier lu par le moteur ;
ClickHouse ClusterIP ; Metabase derrière `loadBalancerSourceRanges` ; le
cloisonnement reste celui de 50_droits.sql — démontré par `kubectl exec`)

## 20. La nuit, sans superviseur
(CronJob : Forbid, backoffLimit, historique ; ce que supervision.py faisait et
pourquoi il n'a plus sa place dans un pod ; l'alerte est un état du Job)

## 21. Limites et coût
(H2 ; un nœud, aucune HA ; pas de TLS devant Metabase ; pas d'observabilité
centralisée ; état Terraform local ; coût mesuré après la démonstration —
compléter avec le chiffre réel de `az consumption usage list` en Task 9)
```

- [ ] **Step 3 : commit**

```bash
git add README.md docs/RAPPORT.md
git commit -m "docs(cloud): décrire le déploiement Azure — choix, sécurité, nuit, limites"
```

---

### Task 9 : déploiement réel, preuve, destruction

**Files:**
- Modify: `docs/RAPPORT.md` § 21 (coût réel), `README.md` si un message d'erreur rencontré mérite le tableau « Reprise sur incident »
- Éventuellement: corrections des Tasks 5-7 révélées par l'exécution

Cette tâche dépense du crédit (≈ 0,15 €/heure de cluster). L'enchaîner sans pause et détruire à la fin.

- [ ] **Step 1 : préparer `terraform.tfvars`**

```bash
cat > infra/terraform/terraform.tfvars <<EOF
subscription_id = "$(az account show --query id -o tsv)"
ip_autorisee    = "$(curl -s https://api.ipify.org)/32"
EOF
git status --short | grep -c tfvars   # doit afficher 0 : le fichier est ignoré
```

- [ ] **Step 2 : déployer**

Run : `time ops/cloud.sh deployer 2>&1 | tee /private/tmp/claude-501/-Users-periicles-Dev-BigData/4f1d301a-cd1e-4f2c-b12d-bb1101ed472d/scratchpad/deployer.log | tail -40`

Expected : se termine par `déployé — image eds-pipeline:<sha>`. Sinon, lire le journal ; causes probables et remèdes :
- `403` sur un secret Key Vault : propagation RBAC trop lente, relancer `ops/cloud.sh deployer` (Terraform reprend où il en était).
- `QuotaExceeded` : vérifier `az vm list-usage -l francecentral -o table | grep -i bs`.
- Pod ClickHouse `ContainerCreating` durable : `kubectl -n eds describe pod clickhouse-0` ; si le volume `secrets` échoue, `kubectl -n eds get secretproviderclass -o yaml` et vérifier `csi_client_id`.

- [ ] **Step 3 : vérifier l'état**

```bash
ops/cloud.sh etat
kubectl -n eds get secret eds-secrets -o jsonpath='{.data}' | python3 -c "import sys,json;print(sorted(json.load(sys.stdin)))"
kubectl -n eds exec clickhouse-0 -- sh -c 'clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" -q "SELECT name FROM system.named_collections"'
```

Expected : pod `clickhouse-0` `Running 1/1` ; les huit clés du Secret ; `eds_lake`.

- [ ] **Step 4 : charger**

Run : `ops/cloud.sh charger 2>&1 | tail -30`

Expected : journaux du pipeline se terminant par l'étape `droits` réussie, code 0. Les jours du dépôt apparaissent ; `bronze chargé` pour chaque source. Si `ClickHouse ne voit pas le lake`, le message dit quoi vérifier (named collection). Si `copie lake` échoue avec une erreur de permission sur `/data/lake`, vérifier le rôle `kubelet_blob` : `az role assignment list --scope $(terraform -chdir=infra/terraform output -raw storage_account_name 2>/dev/null) -o table`.

- [ ] **Step 5 : prouver le cloisonnement depuis le cluster**

```bash
kubectl -n eds exec clickhouse-0 -- sh -c 'clickhouse-client --user eds_pilotage --password "$(cat /mnt/secrets/ch-pilotage-password)" -q "SELECT count() FROM gold_recherche.coh_prevalence"' ; echo "code $?"
kubectl -n eds exec clickhouse-0 -- sh -c 'clickhouse-client --user eds_pilotage --password "$(cat /mnt/secrets/ch-pilotage-password)" -q "SELECT count() FROM gold_pilotage.kpi_dms_service"'
```

Expected : la première commande échoue avec `Not enough privileges` (code non nul), la seconde renvoie un nombre. Conserver la sortie exacte pour le rapport (§ 19).

- [ ] **Step 6 : restituer et vérifier Metabase**

Run : `ops/cloud.sh restituer 2>&1 | tail -15`

Expected : `tableaux de bord` provisionnés, puis l'adresse `http://<ip>:3000`. Ouvrir dans le navigateur, se connecter avec le compte administrateur (mot de passe par la commande affichée), vérifier les trois tableaux de bord. Prendre les captures pour `docs/imgs/` (nommées `aks-metabase-*.png`, sans donnée identifiante).

- [ ] **Step 7 : vérifier l'idempotence du CronJob à la main**

```bash
kubectl -n eds create job --from=cronjob/eds-nuit eds-nuit-manuel
kubectl -n eds wait --for=condition=complete --timeout=600s job/eds-nuit-manuel
kubectl -n eds logs job/eds-nuit-manuel | tail -5
```

Expected : exécution incrémentale sans nouveau jour ; silver et gold reconstruits ; code 0.

- [ ] **Step 8 : relever le coût et compléter le rapport**

```bash
az consumption usage list --start-date $(date -v-1d +%F) --end-date $(date +%F) --query "sum([].pretaxCost)" -o tsv
```

(La consommation remonte avec quelques heures de retard ; noter la valeur, ou l'estimation de la spec si elle est encore nulle, dans `docs/RAPPORT.md` § 21 avec la date du relevé.)

- [ ] **Step 9 : détruire**

Run : `ops/cloud.sh detruire --oui 2>&1 | tail -5`

Expected : `détruit`. Vérifier : `az group list -o table` ne montre plus `rg-eds-cloud` ; `az keyvault list-deleted -o table` ne montre plus le coffre (purgé) — sinon `az keyvault purge --name <kv>`.

- [ ] **Step 10 : commit des corrections et du rapport**

```bash
git status --short
git add docs/RAPPORT.md docs/imgs README.md infra ops eds tests
git commit -m "docs(cloud): preuves du déploiement — cloisonnement, tableaux de bord, coût"
```

S'assurer une dernière fois : `git ls-files | grep -E "tfstate|tfvars$|k8s/rendu"` ne renvoie rien.
