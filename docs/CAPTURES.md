# Captures à réaliser pour le rapport

Les huit premières suffisent à couvrir les huit critères d'évaluation. Les
suivantes renforcent, sans être indispensables à un rapport « court ».

**Avant de commencer** : `docker compose up -d` puis agrandir la police du
terminal (`Cmd +`). Les captures 2 à 7 s'obtiennent avec
`bash ops/captures.sh <n>`.

---

## Essentielles

### 1 — La pseudonymisation, source contre lake  ★ la plus parlante
`bash ops/captures.sh 1`

Montre côte à côte la ligne source — `patient_id`, `nir`, `nom`, `prenom`,
`birth_date` — et la ligne du lake, réduite à `patient_pseudo`, `birth_year`,
`sex`, `region_code`. Les colonnes identifiantes **n'existent plus** : ce
n'est pas un masquage d'affichage.

> Les valeurs réelles ne sont pas reproduites ici : ce document est versionné,
> et l'historique Git doit rester exempt de donnée identifiante. Elles
> apparaissent à l'écran au moment de la capture, et dans le rapport.
→ *Critère RGPD · bonus §7*

### 2 — La preuve de non-fuite
`bash ops/captures.sh 2`

Trois contrôles au vert sur 17 503 valeurs identifiantes cherchées dans tout le
lake. Cadrer la ligne « 6000 patients, 17503 valeurs identifiantes contrôlées ».
→ *Critère RGPD*

### 3 — L'équation de conservation
`bash ops/captures.sh 3`

`silver + rejets = bronze` pour les trois sources. Cadrer au moins le premier
bloc : c'est la preuve qu'aucune ligne ne disparaît silencieusement.
→ *Critère Qualité des traitements*

### 4 — Le cloisonnement, refusé par le moteur
`bash ops/captures.sh 4`

Huit tentatives d'accès croisées : chaque compte lit sa base et se voit refuser
les trois autres. Cadrer les deux blocs de comptes.
→ *Critère RGPD · livrable explicite de la Partie 1*

### 5 — Le cloisonnement vu depuis Metabase  ★ la plus convaincante
Manuel — http://localhost:3000, connecté en admin :

- **+ Nouveau → Question SQL**
- Choisir la base **« EDS — Pilotage hospitalier »** (le choix de la base
  est le cœur de la démonstration : sur la connexion Recherche, la requête
  aboutirait)
- Coller la requête ci-dessous **seule**, sans rien d'autre :

```sql
SELECT count() FROM gold_recherche.coh_prevalence
```

- Exécuter. Résultat attendu :

```
Code: 497 … eds_pilotage: Not enough privileges
```

Prouve que le cloisonnement tient **même pour quelqu'un qui écrit sa propre
requête** dans l'outil. C'est la capture qui coupe court à l'objection
« votre filtre est contournable ».
→ *Critère RGPD + Restitution*

### 6 — Le tableau de bord Pilotage
http://localhost:3000/dashboard/2

Vue complète, **encart de limites visible en haut**. C'est lui qui montre que
tu connais le périmètre de tes chiffres.
→ *Critère Restitution + Fiabilité des indicateurs*

### 7 — Le tableau de bord Recherche
http://localhost:3000/dashboard/3

Vue complète avec son encart. L'âge n'y apparaît qu'en tranches de 10 ans.
→ *Critère Restitution + RGPD (petits effectifs)*

### 8 — Le pipeline complet
`bash ops/captures.sh 6`

Toutes les étapes avec leurs durées, de `schema` à `restitution`. Cadrer de
« démarrage » à « terminé » pour montrer le temps total.
→ *Critère Automatisation*

---

## Complémentaires

### 9 — Erreurs et reprise
`bash ops/captures.sh 5` — cinq scénarios de panne, tracés, entrepôt intact,
reprise par simple relance.
→ *Critère Automatisation (robustesse)*

### 10 — La traçabilité, en SQL
http://localhost:8123/play — utilisateur `eds_admin` :
```sql
SELECT demarre_a, run_id, etape, statut, lignes, duree_s
FROM ops.executions ORDER BY demarre_a DESC LIMIT 12;
```
→ *Critère Traçabilité*

### 11 — Le partitionnement qui rend le rejeu possible
```sql
SELECT partition, rows AS lignes, formatReadableSize(bytes_on_disk) AS taille
FROM system.parts
WHERE database='bronze' AND table='sejours' AND active ORDER BY partition;
```
→ *Critère Architecture*

### 12 — La panne de capteur  ★ ta trouvaille
```sql
SELECT heart_rate, spo2, count() AS n
FROM bronze.monitoring
WHERE heart_rate NOT BETWEEN 20 AND 250 OR spo2 NOT BETWEEN 50 AND 100
GROUP BY heart_rate, spo2 ORDER BY n DESC;
```
Quatre lignes seulement — (0 ou 500) × (0 ou 120). Des butées, jamais de valeur
intermédiaire : c'est un capteur déconnecté, pas du bruit de mesure.
→ *Critère Qualité des traitements*

### 13 — Les rejets, motivés et comptés
```sql
SELECT source, motif, count() AS lignes
FROM silver.rejets GROUP BY source, motif ORDER BY lignes DESC;
```
→ *Critère Qualité des traitements*

### 14 — Le modèle en étoile
```sql
SELECT name AS table, total_rows AS lignes
FROM system.tables WHERE database='gold_pilotage' ORDER BY name;
```
Trois faits, trois dimensions.
→ *Critère Architecture*

### 15 — Ce que l'étoile permet et qu'un catalogue de KPI interdisait
```sql
SELECT s.service, f.tranche_age,
       round(avg(f.duree_jours), 2) AS dms_jours, count() AS sejours
FROM gold_pilotage.fact_sejour AS f
INNER JOIN gold_pilotage.dim_service AS s ON f.service_code = s.service_code
WHERE f.est_en_cours = 0 AND f.tranche_age != 'inconnu'
GROUP BY s.service, f.tranche_age ORDER BY s.service, f.tranche_age;
```
Un croisement service × tranche d'âge, sans avoir eu à l'anticiper.
→ *Critère Architecture*

### 16 — L'absence de colonnes sensibles en recherche
```sql
DESCRIBE TABLE gold_recherche.coh_description;
```
Ni `birth_year`, ni `patient_pseudo`, ni `region`.
→ *Critère RGPD*

---

## Le schéma d'architecture

**Aucune capture n'est nécessaire.** Le diagramme de `docs/RAPPORT.md` est écrit
en Mermaid : il se rend automatiquement sur GitHub et dans la plupart des
visionneuses Markdown. Si ton rapport final est un PDF, exporte-le depuis
l'aperçu, ou copie le bloc dans <https://mermaid.live> pour un export PNG/SVG.

---

## Identifiants

| | |
|---|---|
| Metabase | http://localhost:3000 — voir `MB_ADMIN_EMAIL` / `MB_ADMIN_PASSWORD` dans `.env` |
| ClickHouse | http://localhost:8123/play — voir `CH_ADMIN_USER` / `CH_ADMIN_PASSWORD` dans `.env` |

> Ne capturez jamais le contenu de `.env` : les mots de passe et le sel de
> pseudonymisation y figurent en clair.
