"""Le pilotage de ClickHouse — découpage du SQL, frontières, référentiels.

Aucun de ces tests n'ouvre de connexion : ce sont les fonctions qui
FABRIQUENT et VALIDENT ce qu'on enverra au moteur. Deux d'entre elles portent
une garantie de sécurité — `valider_jour` et le contrôle de substitution —
parce que le chemin d'un `file()` et le marqueur `{run_id}` sont interpolés,
faute de paramètre lié possible.
"""

from __future__ import annotations

import pytest

from eds import warehouse
from tests.conftest import deposer


# ── Validation du jour de dépôt ──────────────────────────────────────────
@pytest.mark.parametrize("jour", ["2026-08-01", "2026-08-29", "1999-12-31"])
def test_jour_bien_forme_est_accepte(jour):
    assert warehouse.valider_jour(jour) == jour


@pytest.mark.parametrize(
    "hostile",
    [
        "2026-08-01'",                     # sortie de chaîne
        "2026-08-01' OR '1'='1",           # injection classique
        "../../etc/passwd",                # traversée de répertoire
        "2026-08-01; DROP TABLE bronze.sejours",
        "2026-8-1",                        # mois et jour non zéro-remplis
        "26-08-01",
        "2026-08-01 ",
        "",
    ],
)
def test_jour_mal_forme_est_refuse(hostile):
    """Le jour est INTERPOLÉ dans un chemin `file()` : `clickhouse-connect`
    n'accepte pas de paramètre lié à cet endroit. La frontière est donc ici,
    et elle est stricte — tout ce qui n'est pas exactement AAAA-MM-JJ est
    rejeté avant d'atteindre une requête."""
    with pytest.raises(ValueError):
        warehouse.valider_jour(hostile)


# ── Substitutions dans les fichiers SQL ──────────────────────────────────
@pytest.mark.parametrize("admise", ["abc123", "run-id_42", "38.5", "-0.5", "50"])
def test_substitution_admise(admise):
    assert warehouse._SUBSTITUTION_ADMISE.fullmatch(admise)


@pytest.mark.parametrize(
    "refusee",
    ["a'b", "a b", "a;b", "a(b)", "'; DROP TABLE x; --", "38,5", "*"],
)
def test_substitution_refusee(refusee):
    """Ni quote, ni espace, ni parenthèse, ni point-virgule ne doivent passer :
    les seuils d'alerte viennent de l'environnement, donc de l'extérieur."""
    assert warehouse._SUBSTITUTION_ADMISE.fullmatch(refusee) is None


def test_executer_fichier_refuse_une_substitution_hostile(tmp_path, monkeypatch):
    """Le refus doit se produire AVANT toute exécution : un client qui lèverait
    à la première commande prouverait seulement que le SQL est invalide, pas
    que la valeur a été bloquée à la frontière."""
    monkeypatch.setattr(warehouse, "SQL_DIR", tmp_path)
    (tmp_path / "essai.sql").write_text("SELECT {seuil};", encoding="utf-8")

    class ClientQuiRefuseDEtreAppele:
        def command(self, *_):
            raise AssertionError("aucune commande ne doit partir")

    with pytest.raises(ValueError, match="Substitution SQL refusée"):
        warehouse.executer_fichier(
            ClientQuiRefuseDEtreAppele(), "essai.sql", seuil="1; DROP TABLE x"
        )


def test_executer_fichier_substitue_et_execute(tmp_path, monkeypatch):
    monkeypatch.setattr(warehouse, "SQL_DIR", tmp_path)
    (tmp_path / "essai.sql").write_text(
        "SELECT '{run_id}';\nSELECT {seuil};", encoding="utf-8")

    envoyees = []

    class ClientEspion:
        def command(self, sql):
            envoyees.append(sql)

    assert warehouse.executer_fichier(
        ClientEspion(), "essai.sql", run_id="abc123", seuil="38.5") == 2
    assert "abc123" in envoyees[0]
    assert "38.5" in envoyees[1]


# ── Découpage du SQL ─────────────────────────────────────────────────────
def test_decoupage_simple():
    assert warehouse.decouper_instructions("SELECT 1; SELECT 2;") == [
        "SELECT 1", "SELECT 2"]


def test_point_virgule_dans_un_commentaire_ne_coupe_pas():
    """Un `split(';')` naïf est FAUX : le caractère apparaît aussi dans les
    commentaires, dont ce projet est rempli."""
    sql = "SELECT 1 -- un point-virgule ; ici\n, 2;"
    assert warehouse.decouper_instructions(sql) == [
        "SELECT 1 -- un point-virgule ; ici\n, 2"]


def test_point_virgule_dans_un_commentaire_bloc_ne_coupe_pas():
    sql = "SELECT /* ; toujours pas ; */ 1;"
    assert len(warehouse.decouper_instructions(sql)) == 1


def test_point_virgule_dans_une_chaine_ne_coupe_pas():
    sql = "SELECT 'valeur ; avec point-virgule';"
    assert warehouse.decouper_instructions(sql) == [
        "SELECT 'valeur ; avec point-virgule'"]


def test_bloc_de_commentaires_seuls_est_ecarte():
    """Un bloc qui ne contient QUE des commentaires ne part pas au moteur.

    C'est le cas de la queue d'un fichier SQL, après le dernier `;` : sans ce
    filtre, chaque fichier de ce projet enverrait une instruction vide.
    """
    sql = "SELECT 1;\n-- une note de fin de fichier\n-- et sa suite\n"
    assert warehouse.decouper_instructions(sql) == ["SELECT 1"]


def test_en_tete_de_commentaires_reste_attache_a_son_instruction():
    """Comportement VOULU, et pas un oubli : les commentaires de ce projet
    portent la justification des choix, et ClickHouse les accepte devant une
    instruction. Les détacher demanderait de réécrire le SQL sans y gagner."""
    sql = "-- pourquoi cette requête\nSELECT 1;"
    assert warehouse.decouper_instructions(sql) == [
        "-- pourquoi cette requête\nSELECT 1"]


def test_sql_vide_ne_produit_aucune_instruction():
    assert warehouse.decouper_instructions("") == []
    assert warehouse.decouper_instructions("\n\n  \n") == []


def test_derniere_instruction_sans_point_virgule_final():
    assert warehouse.decouper_instructions("SELECT 1;\nSELECT 2") == [
        "SELECT 1", "SELECT 2"]


def test_les_fichiers_sql_du_projet_se_decoupent(tmp_path):
    """Contrôle de non-régression sur le SQL RÉEL : chaque fichier doit
    produire au moins une instruction, et aucune ne doit être un bloc de
    commentaires déguisé."""
    from eds.config import RACINE

    for fichier in sorted((RACINE / "sql").glob("*.sql")):
        instructions = warehouse.decouper_instructions(
            fichier.read_text(encoding="utf-8"))
        assert instructions, f"{fichier.name} ne produit aucune instruction"
        for i in instructions:
            assert not warehouse._seulement_commentaires(i)


# ── Résolution des référentiels ──────────────────────────────────────────
def test_referentiel_resolu_sur_son_depot_le_plus_recent(lake_factice):
    """Un référentiel redéposé remplace le précédent. Prendre le PREMIER jour
    — ce que faisait le code avant l'évolution — figerait la nomenclature à sa
    version initiale, sans que rien n'en signale l'âge."""
    deposer(lake_factice, "referentiels/2026-08-01/services.csv", "a\n")
    deposer(lake_factice, "referentiels/2026-08-29/services.csv", "b\n")
    assert warehouse._dernier_depot(
        ["2026-08-01", "2026-08-29"], "services.csv") == "2026-08-29"


def test_chaque_referentiel_a_son_propre_depot(lake_factice):
    """LE CAS QUI A MOTIVÉ LA CORRECTION. Les référentiels n'arrivent pas
    ensemble : résoudre par JOUR en aurait forcément perdu deux."""
    deposer(lake_factice, "referentiels/2026-08-01/services.csv", "")
    deposer(lake_factice, "referentiels/2026-08-01/cim10.csv", "")
    deposer(lake_factice, "referentiels/2026-08-29/ccam.csv", "")
    deposer(lake_factice, "referentiels/2026-08-29/description_service.csv", "")

    jours = ["2026-08-01", "2026-08-29"]
    resolus = {f: warehouse._dernier_depot(jours, f)
               for f in ("services.csv", "cim10.csv",
                         "ccam.csv", "description_service.csv")}
    assert resolus == {
        "services.csv": "2026-08-01",
        "cim10.csv": "2026-08-01",
        "ccam.csv": "2026-08-29",
        "description_service.csv": "2026-08-29",
    }


def test_referentiel_absent_ne_resout_rien(lake_factice):
    """Renvoyer None, et non le dernier jour connu : `charger_referentiels`
    s'en sert pour SIGNALER et laisser la table intacte, plutôt que de la
    tronquer faute de fichier."""
    deposer(lake_factice, "referentiels/2026-08-01/services.csv", "")
    assert warehouse._dernier_depot(["2026-08-01"], "ccam.csv") is None


def test_l_ordre_des_jours_fournis_n_influe_pas(lake_factice):
    deposer(lake_factice, "referentiels/2026-08-01/services.csv", "")
    deposer(lake_factice, "referentiels/2026-08-29/services.csv", "")
    desordre = ["2026-08-29", "2026-08-01"]
    assert warehouse._dernier_depot(desordre, "services.csv") == "2026-08-29"


def test_les_quatre_referentiels_sont_declares():
    """Chaque entrée porte sa table, son fichier, le schéma du CSV et la
    projection insérée — quatre éléments, sans quoi le chargement se ferait
    en `SELECT *` et le typage serait deviné."""
    fichiers = {f for _, f, _, _ in warehouse.REFERENTIELS}
    assert fichiers == {"services.csv", "cim10.csv",
                        "ccam.csv", "description_service.csv"}
    for entree in warehouse.REFERENTIELS:
        assert len(entree) == 4


def test_actes_a_son_chargeur_bronze():
    table, fabriquer = warehouse.CHARGEURS["actes"]
    assert table == "bronze.actes"
    sql = fabriquer("2026-08-29", "run42")
    assert "bronze.actes" in sql and "actes.parquet" in sql
    assert "2026-08-29" in sql and "run42" in sql


# ── Niveau de journalisation ─────────────────────────────────────────────
def test_source_absente_est_une_information_pas_un_avertissement(lake_factice, caplog):
    """Le calendrier de dépôt du CHU est irrégulier PAR CONCEPTION : `patients`
    est un snapshot, `actes` n'est déposée qu'une fois. Une source sans dépôt
    un jour donné est donc le cas nominal, pas une anomalie.

    Un `WARNING` qui décrit le normal use le signal : sur l'historique du
    projet, 7 421 avertissements sur 7 422 portaient ce seul motif, et le
    seul avertissement réel y était noyé. Le niveau `WARNING` doit rester
    réservé à ce qui mérite d'être lu.
    """
    import logging

    caplog.set_level(logging.INFO, logger="eds.warehouse")
    # Aucune source dans le lake : aucun chargement, donc le client n'est
    # jamais sollicité — la fonction se contente de journaliser les absences.
    resultats = warehouse.charger_bronze_jour(None, "2026-08-01", "run-de-test")

    assert resultats == {}
    absences = [r for r in caplog.records if r.message == "source absente"]
    assert len(absences) == len(warehouse.CHARGEURS)
    assert {r.levelname for r in absences} == {"INFO"}


# ── Lecture du lake par le moteur ────────────────────────────────────────
def test_lecteur_fichier_produit_un_file_avec_structure(env_vierge):
    table, provenance = warehouse.source_lake(
        "patients/2026-08-26/patients.csv", "CSVWithNames", "patient_pseudo String"
    )
    assert table == (
        "file('lake/patients/2026-08-26/patients.csv', CSVWithNames, 'patient_pseudo String')"
    )
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
    assert table == (
        "azureBlobStorage(eds_lake, blob_path='actes/2026-08-29/actes.parquet', format='Parquet')"
    )


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
        assert "replaceOne(_path" not in sql


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
