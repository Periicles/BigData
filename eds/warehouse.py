"""Accès à ClickHouse et chargement de la couche bronze.

Principe imposé par le sujet : la transformation s'exécute DANS le moteur.
Python n'envoie que du SQL — les données ne transitent jamais par sa mémoire.
ClickHouse lit lui-même les fichiers du lake, monté en lecture seule dans son
répertoire `user_files`.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from eds.config import LAKE, RACINE, exiger

journal = logging.getLogger(__name__)

SQL_DIR = RACINE / "sql"

# Chemin du lake tel que ClickHouse le voit (cf. volume dans docker-compose).
LAKE_CH = "lake"

_FORMAT_JOUR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def valider_jour(jour: str) -> str:
    """Valide un jour de dépôt avant toute interpolation dans du SQL.

    Les chemins `file()` n'acceptent pas de paramètre lié : le jour est
    interpolé. Il est donc validé strictement à la frontière.
    """
    if not _FORMAT_JOUR.match(jour):
        raise ValueError(f"Jour de dépôt invalide : {jour!r} (attendu AAAA-MM-JJ)")
    return jour


def client() -> Client:
    return clickhouse_connect.get_client(
        host="localhost",
        port=8123,
        username=exiger("CH_ADMIN_USER"),
        password=exiger("CH_ADMIN_PASSWORD"),
    )


# Un identifiant (run_id, mot de passe généré) ou un nombre décimal (seuil).
_SUBSTITUTION_ADMISE = re.compile(r"[A-Za-z0-9_-]+|-?\d+\.\d+")


def executer_fichier(ch: Client, nom: str, **substitutions: str) -> int:
    """Exécute un fichier .sql du répertoire `sql/`, instruction par instruction.

    Les marqueurs `{cle}` du fichier sont remplacés par les substitutions
    fournies. Chaque valeur est validée : un identifiant alphanumérique, ou un
    nombre décimal (les seuils d'alerte). Ni quote, ni espace, ni parenthèse,
    ni point-virgule ne passent : aucune injection n'est possible par ce canal.
    """
    contenu = chemin_sql(nom).read_text(encoding="utf-8")
    for cle, valeur in substitutions.items():
        if not _SUBSTITUTION_ADMISE.fullmatch(valeur):
            raise ValueError(f"Substitution SQL refusée : {cle}={valeur!r}")
        contenu = contenu.replace("{" + cle + "}", valeur)

    instructions = decouper_instructions(contenu)
    for instruction in instructions:
        ch.command(instruction)
    journal.info(
        "sql exécuté", extra={"fichier": nom, "instructions": len(instructions)}
    )
    return len(instructions)


# Commentaire ligne, commentaire bloc, chaîne littérale, ou séparateur. Ces
# quatre motifs sont reconnus dans cet ordre : un ';' à l'intérieur d'un
# commentaire ou d'une chaîne est donc consommé par le motif englobant et ne
# peut plus être vu comme un séparateur.
_JETONS_SQL = re.compile(r"--[^\n]*|/\*.*?\*/|'(?:[^'\\]|\\.)*'|;", re.S)


def decouper_instructions(sql: str) -> list[str]:
    """Découpe un script SQL en instructions.

    Un simple split sur ';' est faux : le caractère apparaît aussi dans les
    commentaires et les chaînes littérales. On ne coupe donc que sur les ';'
    qui subsistent une fois ceux-là reconnus comme des blocs insécables.
    """
    instructions, debut = [], 0
    for jeton in _JETONS_SQL.finditer(sql):
        if jeton.group() == ";":
            instructions.append(sql[debut : jeton.start()])
            debut = jeton.end()
    instructions.append(sql[debut:])
    return [
        s.strip() for s in instructions if s.strip() and not _seulement_commentaires(s)
    ]


def chemin_sql(nom: str) -> Path:
    return SQL_DIR / nom


def _seulement_commentaires(bloc: str) -> bool:
    return all(not l.strip() or l.strip().startswith("--") for l in bloc.splitlines())


# ── Chargement bronze ────────────────────────────────────────────────────
# Une requête par source. Le schéma des fichiers est déclaré explicitement :
# on ne laisse pas ClickHouse deviner les types.


def _sql_patients(jour: str, run_id: str) -> str:
    # birth_year est lu en STRING puis converti en mode TOLÉRANT
    # (`toUInt16OrNull`) : une date de naissance illisible produit une valeur
    # vide dans le lake (cf. eds/lake.py `annee_naissance`), donc un NULL ici
    # — au lieu de faire échouer le chargement du jour ENTIER. Le patient
    # reste conservé, silver le trace en quarantaine.
    return f"""
    INSERT INTO bronze.patients
    SELECT patient_pseudo, toUInt16OrNull(birth_year), sex, region_code,
           toDate('{jour}'), replaceOne(_path, '/var/lib/clickhouse/user_files/', ''), now(), '{run_id}'
    FROM file('{LAKE_CH}/patients/{jour}/patients.csv', CSVWithNames,
              'patient_pseudo String, birth_year String,
               sex String, region_code String')
    """


def _sql_sejours(jour: str, run_id: str) -> str:
    # Les deux dates sont lues en String puis converties en mode TOLÉRANT
    # (`...OrNull`) : une date illisible produit un NULL et sera écartée par
    # silver, au lieu de faire échouer le chargement du jour entier.
    #
    # Sur discharge_ts, le NULL est ambigu — chaîne vide (séjour en cours,
    # légitime) ou date illisible (anomalie). Le drapeau les sépare, sinon une
    # date corrompue passerait silencieusement pour un séjour en cours.
    return f"""
    INSERT INTO bronze.sejours
    SELECT stay_id, patient_pseudo, service_code,
           parseDateTimeBestEffortOrNull(admission_ts),
           parseDateTimeBestEffortOrNull(nullIf(discharge_ts, '')),
           discharge_ts != ''
               AND parseDateTimeBestEffortOrNull(discharge_ts) IS NULL,
           admission_mode, discharge_mode,
           toDate('{jour}'), replaceOne(_path, '/var/lib/clickhouse/user_files/', ''), now(), '{run_id}'
    FROM file('{LAKE_CH}/sejours/{jour}/sejours.csv', CSVWithNames,
              'stay_id String, service_code String, admission_ts String,
               discharge_ts String, admission_mode String,
               discharge_mode String, patient_pseudo String')
    """


def _sql_diagnostics(jour: str, run_id: str) -> str:
    # Aplatissement du JSON imbriqué par ARRAY JOIN, dans le moteur.
    return f"""
    INSERT INTO bronze.diagnostics
    SELECT stay_id, d.code_cim10, d.type,
           toDate('{jour}'), replaceOne(_path, '/var/lib/clickhouse/user_files/', ''), now(), '{run_id}'
    FROM file('{LAKE_CH}/diagnostics/{jour}/diagnostics.json', JSONEachRow,
              'stay_id String,
               diagnostics Array(Tuple(code_cim10 String, type String))')
    ARRAY JOIN diagnostics AS d
    """


def _sql_monitoring(jour: str, run_id: str) -> str:
    return f"""
    INSERT INTO bronze.monitoring
    SELECT stay_id, ts, toInt16(heart_rate), toInt16(spo2), toDecimal32(temp_c, 1),
           toDate('{jour}'), replaceOne(_path, '/var/lib/clickhouse/user_files/', ''), now(), '{run_id}'
    FROM file('{LAKE_CH}/monitoring/{jour}/monitoring.parquet', Parquet)
    """


def _sql_actes(jour: str, run_id: str) -> str:
    # Le parquet porte déjà un TIMESTAMP typé : aucune conversion tolérante
    # n'est nécessaire, contrairement aux CSV de séjours. Le fichier ne
    # contient que stay_id, code_ccam et acte_ts — aucune identité.
    return f"""
    INSERT INTO bronze.actes
    SELECT stay_id, code_ccam, acte_ts,
           toDate('{jour}'), replaceOne(_path, '/var/lib/clickhouse/user_files/', ''), now(), '{run_id}'
    FROM file('{LAKE_CH}/actes/{jour}/actes.parquet', Parquet)
    """


CHARGEURS = {
    "patients": ("bronze.patients", _sql_patients),
    "sejours": ("bronze.sejours", _sql_sejours),
    "diagnostics": ("bronze.diagnostics", _sql_diagnostics),
    "monitoring": ("bronze.monitoring", _sql_monitoring),
    "actes": ("bronze.actes", _sql_actes),
}


def charger_bronze_jour(ch: Client, jour: str, run_id: str) -> dict[str, int]:
    """Charge en bronze les quatre sources journalières d'un jour de dépôt.

    Idempotent : la partition du jour est supprimée avant réinsertion. Rejouer
    un jour déjà chargé donne exactement le même état, sans doublon.
    """
    valider_jour(jour)
    resultats: dict[str, int] = {}
    for source, (table, fabriquer_sql) in CHARGEURS.items():
        if not (LAKE / source / jour).is_dir():
            # INFO, et non WARNING. Le calendrier de dépôt du CHU est
            # irrégulier PAR CONCEPTION — `patients` est un snapshot, `actes`
            # n'est déposée qu'une fois — donc une source sans dépôt un jour
            # donné est le cas nominal. Un avertissement qui décrit le normal
            # use le signal : l'exploitant cesse de lire des `WARNING` dont
            # aucun ne mérite d'être lu, et manque le jour où il y en a un.
            journal.info("source absente", extra={"source": source, "jour": jour})
            continue

        ch.command(f"ALTER TABLE {table} DROP PARTITION '{jour}'")
        ch.command(fabriquer_sql(jour, run_id))
        lignes = ch.command(
            f"SELECT count() FROM {table} WHERE _jour_depot = toDate('{jour}')"
        )
        resultats[source] = int(lignes)
        journal.info(
            "bronze chargé", extra={"source": source, "jour": jour, "lignes": lignes}
        )
    return resultats


# Chaque référentiel est un FICHIER, pas un jour de dépôt : ils n'arrivent pas
# tous ensemble. `services.csv` et `cim10.csv` sont déposés le premier jour,
# `ccam.csv` et `description_service.csv` au dépôt d'évolution du 29 août.
#
# Pour chacun : le schéma du CSV, puis la projection insérée. Les conversions
# numériques sont TOLÉRANTES — une valeur illisible entre en NULL et sera
# tracée, au lieu de faire échouer le chargement de la nomenclature entière.
REFERENTIELS = (
    (
        "bronze.ref_services",
        "services.csv",
        "service_code String, service_label String",
        "service_code, service_label",
    ),
    (
        "bronze.ref_cim10",
        "cim10.csv",
        "code_cim10 String, libelle String",
        "code_cim10, libelle",
    ),
    (
        "bronze.ref_ccam",
        "ccam.csv",
        "code_ccam String, libelle String, tarif_euros String",
        "code_ccam, libelle, toDecimal32OrNull(tarif_euros, 2)",
    ),
    (
        "bronze.ref_description_service",
        "description_service.csv",
        "service_code String, categorie String, capacite_lits String, pole String",
        "service_code, categorie, toUInt16OrNull(capacite_lits), pole",
    ),
)


def _dernier_depot(jours: list[str], fichier: str) -> str | None:
    """Dépôt le plus récent qui fournit ce référentiel, s'il en existe un.

    Le plus récent et non le premier : un référentiel redéposé remplace le
    précédent. Prendre le premier figerait la nomenclature à sa version
    initiale, sans que rien n'en signale l'âge.
    """
    for jour in sorted(jours, reverse=True):
        if (LAKE / "referentiels" / jour / fichier).is_file():
            return jour
    return None


def charger_referentiels(ch: Client, jours: list[str], run_id: str) -> dict[str, int]:
    """Recharge intégralement les référentiels, chacun sur son dépôt le plus récent.

    Ils sont hors du flux incrémental journalier : les traiter comme les
    autres sources laisserait un pipeline démarré plus tard sans nomenclature.

    Un référentiel qu'aucun dépôt ne fournit est SIGNALÉ et laissé intact —
    pas vidé. Tronquer une table faute de fichier remplacerait une nomenclature
    utilisable par une table vide, sans rien apporter.
    """
    for jour in jours:
        valider_jour(jour)

    resultats: dict[str, int] = {}
    for table, fichier, colonnes, projection in REFERENTIELS:
        jour = _dernier_depot(jours, fichier)
        if jour is None:
            journal.warning(
                "référentiel absent du lake",
                extra={"table": table, "fichier": fichier},
            )
            continue

        ch.command(f"TRUNCATE TABLE {table}")
        ch.command(f"""
            INSERT INTO {table}
            SELECT {projection},
                   replaceOne(_path, '/var/lib/clickhouse/user_files/', ''), now(), '{run_id}'
            FROM file('{LAKE_CH}/referentiels/{jour}/{fichier}', CSVWithNames, '{colonnes}')
        """)
        resultats[table] = int(ch.command(f"SELECT count() FROM {table}"))
        journal.info(
            "référentiel chargé",
            extra={"table": table, "jour": jour, "lignes": resultats[table]},
        )
    return resultats
