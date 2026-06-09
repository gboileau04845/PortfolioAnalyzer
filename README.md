# Fabric Data Agent — Chat

Mini-app de chat (FastAPI + HTML/JS) qui relaie les questions des utilisateurs
vers un **Microsoft Fabric Data Agent** via son endpoint OpenAI-compatible, et
réaffiche les réponses. Authentification par **identité utilisateur** (connexion
Entra ID dans le navigateur) — le data agent tourne avec les droits de l'utilisateur.

```
Navigateur ──► FastAPI (/api/chat) ──► Fabric Data Agent (endpoint OpenAI) ──► sources de données
   (UI chat)      (1 thread Fabric/session)         (assistant → thread → run)
        │
        └─ connexion Entra ID (navigateur) → token utilisateur → droits respectés
```

## 1. Configuration

```powershell
Copy-Item .env.example .env
```

Puis éditer `.env` :

- `AZURE_TENANT_ID` — recommandé (ton tenant).
- `AZURE_CLIENT_ID` — optionnel (voir section auth).
- `DATA_AGENT_URL` — déjà prérempli avec ton endpoint.

> **Ton compte doit avoir accès au data agent ET à ses sources de données**
> (lakehouse / warehouse / semantic model / KQL…), sinon les requêtes échouent.

## 2. Installation

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## 3. Lancement

```powershell
.\.venv\Scripts\Activate.ps1
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Ouvrir http://127.0.0.1:8000

## Authentification : identité utilisateur

L'app utilise `InteractiveBrowserCredential` : à la première question, un
navigateur s'ouvre pour ta connexion Entra ID. Le data agent s'exécute ensuite
avec **tes droits** (approche recommandée par Microsoft).

> ⚠️ **Mono-utilisateur.** `InteractiveBrowserCredential` authentifie *le* compte
> qui se connecte dans le navigateur ouvert par le serveur. Parfait pour un test
> local. Pour une vraie app **multi-utilisateurs**, il faut un flux OAuth
> Authorization Code (sign-in par utilisateur via MSAL) — seul `_credential` /
> `_token()` dans `fabric_agent.py` est à changer.

Si tu obtiens un **401/403** ou une erreur **AADSTS** :

1. Vérifier que ton compte a accès au data agent et à ses sources.
2. Essayer l'autre scope dans `.env` :
   `FABRIC_SCOPE=https://analysis.windows.net/powerbi/api/.default`.
3. Erreur sur le client/redirect → renseigner `AZURE_CLIENT_ID` (app
   registration client public avec redirect URI `http://localhost` et
   permissions déléguées Fabric/Power BI).

## Fichiers

| Fichier | Rôle |
|---|---|
| `app.py` | Backend FastAPI : routes `/`, `/api/chat`, `/api/reset`, gestion de session |
| `fabric_agent.py` | Client du data agent (auth SPN + flux Assistants OpenAI) |
| `templates/index.html` | UI de chat (HTML/CSS/JS inline) |
| `.env` | Secrets (non commité) |

## Notes

- Sessions et threads en mémoire processus (perdus au redémarrage) — suffisant
  pour un POC. Persister en base pour la prod.
- Un thread Fabric = une conversation. « Nouvelle conversation » réinitialise.
- `openai` est **pinné en 1.70.0** : l'app utilise des internes du SDK
  (`_prepare_options`) sensibles à la version, validés par Microsoft.
