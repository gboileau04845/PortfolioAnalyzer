#!/bin/bash
# Commande de démarrage pour Azure App Service (Linux, Python).
# À renseigner dans : App Service → Configuration → General settings →
# Startup Command  =  startup.sh   (ou coller la ligne gunicorn ci-dessous).
#
# --timeout 600 : indispensable, le data agent peut répondre en ~120 s
# (gunicorn tue sinon les requêtes longues au bout de 30 s par défaut).
gunicorn app:app \
  --workers 2 \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8000 \
  --timeout 600
