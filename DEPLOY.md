# Déploiement sur Azure App Service (Test & Prod)

Architecture cible : **deux Web Apps** (Test et Prod), chacune branchée à sa
branche GitHub (`Test` / `Prod`). Authentification = **SSO Microsoft** via
App Service Authentication (« Easy Auth »).

## 1. Authentification (SSO) — comment l'app l'utilise
- En **Azure**, Easy Auth authentifie l'utilisateur (SSO Entra ID) et injecte
  son jeton dans l'en-tête `X-MS-TOKEN-AAD-ACCESS-TOKEN` (+ UPN dans
  `X-MS-CLIENT-PRINCIPAL-NAME`). L'app lit ce jeton **par requête** → la RLS
  s'applique avec l'identité de chaque utilisateur.
- En **local**, ces en-têtes sont absents → repli automatique sur
  `InteractiveBrowserCredential` (navigateur). Aucune config requise pour dev.

## 2. Créer les ressources (à répéter pour Test et Prod)
```bash
# Variables d'exemple
RG=rg-portfolio-analyzer
PLAN=asp-portfolio-analyzer
APP=portfolioanalyzer-test          # puis portfolioanalyzer-prod

az group create -n $RG -l westeurope
az appservice plan create -g $RG -n $PLAN --is-linux --sku B1
az webapp create -g $RG -p $PLAN -n $APP --runtime "PYTHON:3.11"

# Commande de démarrage
az webapp config set -g $RG -n $APP --startup-file "startup.sh"
```

## 3. App Settings (par environnement)
App Service → Configuration → Application settings (remplacent le `.env`) :

| Clé | Valeur |
|---|---|
| `DATA_AGENT_URL` | URL OpenAI du data agent (identique Test/Prod) |
| `DATA_AGENT_API_VERSION` | `2024-05-01-preview` |
| `FABRIC_SCOPE` | `https://api.fabric.microsoft.com/.default` |
| `AGENT_TIMEOUT` | `120` |
| `FABRIC_CAPACITY_SKU_CU` | ex. `64` |
| `AGENT_CU_BUDGET_WINDOW_HOURS` | `24` |
| `AGENT_CU_BUDGET_GROUP_PCT` | ex. `10` |
| `AGENT_CU_BUDGET_USER_PCT` | ex. `0.5` |
| `ADMIN_ACCESS_CODE` | code admin (différent Test/Prod conseillé) |
| `DATA_DIR` | `/home/data` (persistant entre redémarrages) |

```bash
az webapp config appsettings set -g $RG -n $APP --settings \
  DATA_AGENT_URL="..." DATA_AGENT_API_VERSION="2024-05-01-preview" \
  FABRIC_SCOPE="https://api.fabric.microsoft.com/.default" AGENT_TIMEOUT=120 \
  FABRIC_CAPACITY_SKU_CU=64 AGENT_CU_BUDGET_WINDOW_HOURS=24 \
  AGENT_CU_BUDGET_GROUP_PCT=10 AGENT_CU_BUDGET_USER_PCT=0.5 \
  ADMIN_ACCESS_CODE="..." DATA_DIR="/home/data"
```

## 4. Activer le SSO (Easy Auth)
App Service → **Authentication** → **Add identity provider** → **Microsoft** :
- **Create new app registration** (une par app = isolation Test/Prod recommandée).
- **Require authentication** (rejeter les non connectés → 302 vers la connexion MS).
- **Token store** : activé.
- **Scopes / login** : inclure la ressource **Fabric** afin que le jeton stocké
  serve à appeler le data agent (sinon prévoir un échange On-Behalf-Of).

Puis sur l'**app registration** : ajouter les **permissions déléguées Fabric /
Power BI** + **consentement administrateur**. Le redirect URI
`https://<app>.azurewebsites.net/.auth/login/aad/callback` est créé
automatiquement par Easy Auth.

## 5. CI/CD (brancher chaque app à sa branche)
App Service → **Deployment Center** → GitHub → repo
`gboileau04845/PortfolioAnalyzer` :
- App **Test** ⟶ branche `Test`
- App **Prod** ⟶ branche `Prod`

Azure génère un workflow GitHub Actions ; chaque push sur la branche déclenche
le build + déploiement.

## 6. Accès aux données
Chaque utilisateur doit avoir, dans Fabric : **Read** sur le data agent, **Read**
sur le modèle sémantique (+ ses sources), et être membre du **rôle RLS** voulu.
