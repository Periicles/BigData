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

from eds.config import RACINE, exiger

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


def executer_fichier(ch: Client, nom: str, **substitutions: str) -> int:
    """Exécute un fichier .sql du répertoire `sql/`, instruction par instruction.

    Les marqueurs `{cle}` du fichier sont remplacés par les substitutions
    fournies. Chaque valeur est validée : seuls les caractères alphanumériques
    et le tiret sont admis, ce qui exclut toute injection par ce canal.
    """
    contenu = chemin_sql(nom).read_text(encoding="utf-8")
    for cle, valeur in substitutions.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", valeur):
            raise ValueError(f"Substitution SQL refusée : {cle}={valeur!r}")
        contenu = contenu.replace("{" + cle + "}", valeur)

    instructions = decouper_instructions(contenu)
    for instruction in instructions:
        ch.command(instruction)
    journal.info(
        "sql exécuté", extra={"fichier": nom, "instructions": len(instructions)}
    )
    return len(instructions)


def decouper_instructions(sql: str) -> list[str]:
    """Découpe un script SQL en instructions.

    Un simple split sur ';' est faux : le caractère apparaît aussi dans les
    commentaires et les chaînes littérales. On parcourt donc le texte en
    suivant l'état courant (commentaire ligne, commentaire bloc, chaîne).
    """
    instructions, courante = [], []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        suivant = sql[i + 1] if i + 1 < n else ""

        if c == "-" and suivant == "-":  # commentaire ligne
            fin = sql.find("\n", i)
            fin = n if fin == -1 else fin
            courante.append(sql[i:fin])
            i = fin
        elif c == "/" and suivant == "*":  # commentaire bloc
            fin = sql.find("*/", i + 2)
            fin = n if fin == -1 else fin + 2
            courante.append(sql[i:fin])
            i = fin
        elif c == "'":  # chaîne littérale
            j = i + 1
            while j < n:
                if sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == "'":
                    break
                j += 1
            courante.append(sql[i : j + 1])
            i = j + 1
        elif c == ";":  # fin d'instruction
            instructions.append("".join(courante))
            courante = []
            i += 1
        else:
            courante.append(c)
            i += 1

    instructions.append("".join(courante))
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
    return f"""
    INSERT INTO bronze.patients
    SELECT patient_pseudo, birth_year, sex, region_code,
           toDate('{jour}'), replaceOne(_path, '/var/lib/clickhouse/user_files/', ''), now(), '{run_id}'
    FROM file('{LAKE_CH}/patients/{jour}/patients.csv', CSVWithNames,
              'patient_pseudo String, birth_year UInt16,
               sex String, region_code String')
    """


def _sql_sejours(jour: str, run_id: str) -> str:
    # discharge_ts est lu en String puis converti : la chaîne vide signifie
    # « séjour en cours » et doit devenir NULL, pas une date par défaut.
    return f"""
    INSERT INTO bronze.sejours
    SELECT stay_id, patient_pseudo, service_code,
           parseDateTimeBestEffort(admission_ts),
           parseDateTimeBestEffortOrNull(nullIf(discharge_ts, '')),
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


CHARGEURS = {
    "patients": ("bronze.patients", _sql_patients),
    "sejours": ("bronze.sejours", _sql_sejours),
    "diagnostics": ("bronze.diagnostics", _sql_diagnostics),
    "monitoring": ("bronze.monitoring", _sql_monitoring),
}


def charger_bronze_jour(ch: Client, jour: str, run_id: str) -> dict[str, int]:
    """Charge en bronze les quatre sources journalières d'un jour de dépôt.

    Idempotent : la partition du jour est supprimée avant réinsertion. Rejouer
    un jour déjà chargé donne exactement le même état, sans doublon.
    """
    valider_jour(jour)
    from eds.config import LAKE

    resultats: dict[str, int] = {}
    for source, (table, fabriquer_sql) in CHARGEURS.items():
        if not (LAKE / source / jour).is_dir():
            journal.warning("source absente", extra={"source": source, "jour": jour})
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


def charger_referentiels(ch: Client, jour: str, run_id: str) -> dict[str, int]:
    """Recharge intégralement les référentiels.

    Ils ne sont déposés que le premier jour : les traiter comme les autres
    sources laisserait un pipeline démarré plus tard sans nomenclature.
    """
    valider_jour(jour)
    resultats = {}
    for table, fichier, colonnes in (
        (
            "bronze.ref_services",
            "services.csv",
            "service_code String, service_label String",
        ),
        ("bronze.ref_cim10", "cim10.csv", "code_cim10 String, libelle String"),
    ):
        ch.command(f"TRUNCATE TABLE {table}")
        ch.command(f"""
            INSERT INTO {table}
            SELECT *, replaceOne(_path, '/var/lib/clickhouse/user_files/', ''), now(), '{run_id}'
            FROM file('{LAKE_CH}/referentiels/{jour}/{fichier}', CSVWithNames, '{colonnes}')
        """)
        resultats[table] = int(ch.command(f"SELECT count() FROM {table}"))
    return resultats
