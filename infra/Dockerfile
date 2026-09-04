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
