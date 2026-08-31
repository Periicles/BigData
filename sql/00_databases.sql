-- Bases de l'entrepôt. Le cloisonnement pilotage/recherche est PHYSIQUE :
-- deux bases distinctes, deux utilisateurs, deux jeux de droits (cf. 50_droits.sql).
CREATE DATABASE IF NOT EXISTS bronze;
CREATE DATABASE IF NOT EXISTS silver;
CREATE DATABASE IF NOT EXISTS gold_pilotage;
CREATE DATABASE IF NOT EXISTS gold_recherche;
CREATE DATABASE IF NOT EXISTS ops;   -- journal d'exécution du pipeline
