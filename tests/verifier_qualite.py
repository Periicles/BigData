"""Contrôles de cohérence de la couche silver.

Le contrôle central est l'ÉQUATION DE CONSERVATION : pour chaque source,
    lignes en bronze = lignes en silver + lignes rejetées
Aucune ligne ne peut donc disparaître silencieusement. C'est la différence
entre écarter des données et les perdre.

Usage :  python -m tests.verifier_qualite
"""
from __future__ import annotations

import sys

from eds.warehouse import client

ROUGE, VERT, RAZ = "\033[31m", "\033[32m", "\033[0m"


def main() -> int:
    ch = client()
    n = lambda requete: int(ch.command(requete))
    echecs = []

    def controle(libelle: str, obtenu, attendu) -> None:
        if obtenu == attendu:
            print(f"{VERT}OK{RAZ}     {libelle:52} {obtenu}")
        else:
            echecs.append(libelle)
            print(f"{ROUGE}ECHEC{RAZ}  {libelle:52} {obtenu} (attendu {attendu})")

    print("── Équation de conservation : bronze = silver + rejets ──\n")
    for source, table in (("sejours", "sejours"),
                          ("diagnostics", "diagnostics"),
                          ("monitoring", "monitoring")):
        bronze = n(f"SELECT count() FROM bronze.{table}")
        silver = n(f"SELECT count() FROM silver.{table}")
        rejets = n(f"SELECT count() FROM silver.rejets WHERE source = '{source}'")
        controle(f"{source} : {silver} + {rejets}", silver + rejets, bronze)

    print("\n── Déduplication du snapshot cumulatif ──\n")
    controle("patients : 16 200 lignes -> patients distincts",
             n("SELECT count() FROM silver.patients"),
             n("SELECT uniqExact(patient_pseudo) FROM bronze.patients"))
    controle("aucun doublon en silver.patients",
             n("SELECT count() - uniqExact(patient_pseudo) FROM silver.patients"), 0)

    print("\n── Règles métier du sujet ──\n")
    controle("séjours en cours conservés (discharge_ts vide)",
             n("SELECT count() FROM silver.sejours WHERE est_en_cours = 1"), 1190)
    controle("aucune durée négative ne subsiste",
             n("SELECT count() FROM silver.sejours WHERE duree_jours < 0"), 0)
    controle("durée NULL si et seulement si séjour en cours",
             n("""SELECT count() FROM silver.sejours
                  WHERE (duree_jours IS NULL) != (est_en_cours = 1)"""), 0)
    controle("mode de sortie vide normalisé en 'inconnu'",
             n("SELECT count() FROM silver.sejours WHERE discharge_mode = ''"), 0)
    controle("relevés hors plage physiologique éliminés",
             n("""SELECT count() FROM silver.monitoring
                  WHERE heart_rate NOT BETWEEN 20 AND 250
                     OR spo2 NOT BETWEEN 50 AND 100
                     OR temp_c NOT BETWEEN 30 AND 45"""), 0)

    print("\n── Intégrité référentielle de silver ──\n")
    controle("aucun séjour orphelin de patient",
             n("""SELECT count() FROM silver.sejours
                  WHERE patient_pseudo NOT IN (SELECT patient_pseudo FROM silver.patients)"""), 0)
    controle("aucun diagnostic orphelin de séjour",
             n("""SELECT count() FROM silver.diagnostics
                  WHERE stay_id NOT IN (SELECT stay_id FROM silver.sejours)"""), 0)
    controle("aucun relevé orphelin de séjour",
             n("""SELECT count() FROM silver.monitoring
                  WHERE stay_id NOT IN (SELECT stay_id FROM silver.sejours)"""), 0)
    controle("aucun service non résolu",
             n("SELECT count() FROM silver.sejours WHERE service_label = 'inconnu'"), 0)
    controle("aucun code CIM-10 non résolu",
             n("SELECT count() FROM silver.diagnostics WHERE libelle = 'inconnu'"), 0)

    print()
    if echecs:
        print(f"{ROUGE}{len(echecs)} contrôle(s) en échec{RAZ}")
        return 1
    print(f"{VERT}Tous les contrôles passent{RAZ}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
