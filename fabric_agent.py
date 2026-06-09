"""Client du Fabric Data Agent via son endpoint OpenAI-compatible.

Flux Assistants OpenAI : assistant -> thread -> message -> run -> poll -> messages.

Authentification = IDENTITE UTILISATEUR (flux délégué). Au premier appel, un
navigateur s'ouvre pour la connexion Entra ID ; le data agent s'exécute ensuite
avec les droits de l'utilisateur connecté et respecte donc ses permissions sur
les sources de données (approche recommandée par Microsoft pour les data agents).

NB : `InteractiveBrowserCredential` authentifie UN utilisateur (celui qui se
connecte dans le navigateur ouvert par le serveur). Parfait pour un test local
mono-utilisateur. Pour une vraie app multi-utilisateurs, remplacer par un flux
OAuth Authorization Code (MSAL, sign-in par utilisateur) — seul `_token()` /
`_credential` est à changer, le reste du code est inchangé.
"""

from __future__ import annotations

import base64
import contextvars
import json
import os
import time
import uuid
import typing as t

from openai import OpenAI
from openai._models import FinalRequestOptions
from openai._types import Omit
from openai._utils import is_given
from azure.identity import InteractiveBrowserCredential

DATA_AGENT_URL = os.environ["DATA_AGENT_URL"].rstrip("/")
API_VERSION = os.environ.get("DATA_AGENT_API_VERSION", "2024-05-01-preview")
SCOPE = os.environ.get("FABRIC_SCOPE", "https://api.fabric.microsoft.com/.default")
AGENT_TIMEOUT = int(os.environ.get("AGENT_TIMEOUT", "120"))

# --- Estimation de la consommation CU du data agent ---------------------
# Taux officiel Microsoft (doc « Data agent in Fabric consumption », opération
# « AI Query ») : facturation au token, la sortie coûtant 4× l'entrée.
#   - entrée  (prompt)     : 100 CU·s / 1 000 tokens
#   - sortie  (complétion) : 400 CU·s / 1 000 tokens
# NB : l'exécution d'éventuelles requêtes DAX/SQL générées par l'agent est
# facturée EN PLUS sur le moteur concerné — non incluse dans cette estimation.
CU_PER_1K_INPUT = 100.0
CU_PER_1K_OUTPUT = 400.0

# Quand l'agent n'expose pas les tokens, on les estime grossièrement à partir
# du texte (~4 caractères par token).
_CHARS_PER_TOKEN = 4


def estimate_cu_seconds(
    tokens_in=None, tokens_out=None, tokens_total=None, question="", answer=""
) -> tuple[float, str]:
    """Estime les CU·s d'un run et renvoie (cu_seconds, base_d_estimation).

    base ∈ {"exact", "total-split", "text-estimate"} pour tracer la fiabilité.
    """
    if tokens_in is not None and tokens_out is not None:
        cu = (tokens_in * CU_PER_1K_INPUT + tokens_out * CU_PER_1K_OUTPUT) / 1000.0
        return round(cu, 2), "exact"
    if tokens_total is not None:
        # Split heuristique entrée/sortie (l'entrée inclut le grounding, donc
        # généralement majoritaire) : 70 % entrée / 30 % sortie.
        ti, to = tokens_total * 0.7, tokens_total * 0.3
        cu = (ti * CU_PER_1K_INPUT + to * CU_PER_1K_OUTPUT) / 1000.0
        return round(cu, 2), "total-split"
    # Dernier recours : estimer les tokens depuis la longueur des textes.
    ti = max(1, len(question) // _CHARS_PER_TOKEN)
    to = max(1, len(answer) // _CHARS_PER_TOKEN)
    cu = (ti * CU_PER_1K_INPUT + to * CU_PER_1K_OUTPUT) / 1000.0
    return round(cu, 2), "text-estimate"

# --- Authentification : deux modes ---------------------------------------
# 1) AZURE (prod/test) : SSO Microsoft via App Service « Easy Auth ». App
#    Service authentifie l'utilisateur et transmet son jeton d'accès dans un
#    en-tête ; l'app le lit par requête (jeton par utilisateur -> la RLS
#    s'applique). app.py dépose ce jeton dans la variable de contexte ci-dessous.
# 2) LOCAL (dev) : aucun en-tête Easy Auth -> repli sur InteractiveBrowserCredential
#    (ouvre le navigateur sur la machine du dev).
_user_token: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "fabric_user_token", default=None
)

_cred_kwargs: dict[str, str] = {}
if os.environ.get("AZURE_TENANT_ID"):
    _cred_kwargs["tenant_id"] = os.environ["AZURE_TENANT_ID"]
if os.environ.get("AZURE_CLIENT_ID"):
    _cred_kwargs["client_id"] = os.environ["AZURE_CLIENT_ID"]

# Credential interactif créé paresseusement (seulement en mode LOCAL, pour ne
# pas tenter d'ouvrir un navigateur côté serveur en mode SSO).
_credential = None


def set_request_token(token: str | None):
    """Dépose le jeton SSO de la requête courante (renvoie le reset token)."""
    return _user_token.set(token)


def reset_request_token(reset_token) -> None:
    _user_token.reset(reset_token)


def _token() -> str:
    """Jeton d'accès Fabric : jeton SSO de la requête si présent, sinon
    repli interactif (dev local)."""
    tok = _user_token.get()
    if tok:
        return tok
    global _credential
    if _credential is None:
        _credential = InteractiveBrowserCredential(**_cred_kwargs)
    return _credential.get_token(SCOPE).token


class FabricOpenAI(OpenAI):
    """Client OpenAI pointé sur le data agent, avec injection du bearer AAD."""

    def __init__(self, api_version: str = API_VERSION, **kwargs: t.Any) -> None:
        self.api_version = api_version
        default_query = kwargs.pop("default_query", {})
        default_query["api-version"] = api_version
        super().__init__(
            api_key="",  # non utilisé : on passe par l'en-tête Authorization
            base_url=DATA_AGENT_URL,
            default_query=default_query,
            **kwargs,
        )

    def _prepare_options(self, options: FinalRequestOptions) -> None:
        headers: dict[str, str | Omit] = (
            {**options.headers} if is_given(options.headers) else {}
        )
        options.headers = headers
        headers["Authorization"] = f"Bearer {_token()}"
        headers.setdefault("Accept", "application/json")
        headers.setdefault("ActivityId", str(uuid.uuid4()))
        return super()._prepare_options(options)


# max_retries=0 : un thread Fabric n'autorise qu'UN run actif. Si le SDK rejoue
# une requête POST (ex. pendant la lenteur de la 1re connexion navigateur), il
# crée un run fantôme qui bloque l'ajout du message suivant. On désactive donc
# les retries automatiques sur les appels à effet de bord.
_client = FabricOpenAI(max_retries=0)
_assistant = None  # créé paresseusement au premier appel

# Statuts d'un run considéré comme "actif" (non terminal).
_ACTIVE_RUN_STATES = {"queued", "in_progress", "requires_action", "cancelling"}


def _get_assistant():
    global _assistant
    if _assistant is None:
        # Le modèle n'est pas utilisé côté data agent (il a sa propre config).
        _assistant = _client.beta.assistants.create(model="not used")
    return _assistant


def _decode_user(token: str) -> dict:
    """Extrait nom/UPN du token AAD (best effort, sans dépendance externe)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # padding base64
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return {
            "name": claims.get("name"),
            "upn": (
                claims.get("upn")
                or claims.get("preferred_username")
                or claims.get("unique_name")
            ),
        }
    except Exception:
        return {}


def login() -> dict:
    """Déclenche la connexion (ouvre le navigateur si besoin) et préchauffe
    l'assistant. Renvoie les infos de l'utilisateur connecté (best effort)."""
    user = _decode_user(_token())
    _get_assistant()  # crée l'assistant tant que le token est chaud
    return user


def logout() -> None:
    """Déconnecte réellement (mode LOCAL) : vide le credential mis en cache pour
    que la prochaine connexion rouvre le navigateur et permette de choisir un
    AUTRE compte. En mode SSO, la déconnexion se fait côté App Service."""
    global _credential, _assistant
    _credential = None
    _assistant = None


def new_thread() -> str:
    """Ouvre un nouveau fil de conversation et renvoie son id."""
    return _client.beta.threads.create().id


def delete_thread(thread_id: str) -> None:
    try:
        _client.beta.threads.delete(thread_id=thread_id)
    except Exception:
        pass  # best effort


def _ensure_no_active_run(thread_id: str, wait: int = 30, poll: int = 1) -> None:
    """Annule/attend tout run encore actif sur le thread (1 seul run autorisé)."""
    try:
        runs = _client.beta.threads.runs.list(thread_id=thread_id, limit=10)
    except Exception:
        return  # thread neuf ou liste indisponible : rien à nettoyer
    start = time.time()
    for run in runs.data:
        if run.status not in _ACTIVE_RUN_STATES:
            continue
        try:
            _client.beta.threads.runs.cancel(thread_id=thread_id, run_id=run.id)
        except Exception:
            pass
        while run.status in _ACTIVE_RUN_STATES and time.time() - start < wait:
            time.sleep(poll)
            try:
                run = _client.beta.threads.runs.retrieve(
                    thread_id=thread_id, run_id=run.id
                )
            except Exception:
                break


def _log(msg: str) -> None:
    print(f"[fabric_agent] {msg}", flush=True)


def _log_run_diagnostics(thread_id: str, run) -> None:
    """Journalise les étapes du run (requêtes générées, erreurs) pour diagnostic."""
    _log(f"  last_error = {getattr(run, 'last_error', None)}")
    try:
        steps = _client.beta.threads.runs.steps.list(
            thread_id=thread_id, run_id=run.id, limit=20
        )
        for s in steps.data:
            _log(f"  step type={s.type} status={s.status} details={getattr(s, 'step_details', None)}")
    except Exception as e:
        _log(f"  (lecture des steps impossible : {e})")


def ask(thread_id: str, question: str, poll_interval: int = 2) -> tuple[str, dict]:
    """Pose une question dans `thread_id`.

    Renvoie un tuple ``(réponse, meta)`` où ``meta`` contient des indicateurs
    d'activité de l'agent pour suivre la charge sur la capacité Fabric :
    ``elapsed`` (durée du run en s) et ``tokens`` (total de tokens du run si
    l'agent les expose, sinon ``None``).
    """
    _ensure_no_active_run(thread_id)
    _client.beta.threads.messages.create(
        thread_id=thread_id, role="user", content=question
    )
    run = _client.beta.threads.runs.create(
        thread_id=thread_id, assistant_id=_get_assistant().id
    )
    _log(f"run {run.id} créé (status={run.status}) — question: {question!r}")

    terminal = {"completed", "failed", "cancelled", "expired", "requires_action"}
    start = time.time()
    last_status = run.status
    while run.status not in terminal:
        if time.time() - start > AGENT_TIMEOUT:
            _log(f"run {run.id} TIMEOUT après {AGENT_TIMEOUT}s (status={run.status})")
            _log_run_diagnostics(thread_id, run)
            try:
                _client.beta.threads.runs.cancel(thread_id=thread_id, run_id=run.id)
            except Exception:
                pass
            raise TimeoutError(
                f"The data agent did not respond within {AGENT_TIMEOUT}s "
                f"(last status: {run.status})."
            )
        time.sleep(poll_interval)
        run = _client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
        if run.status != last_status:
            _log(f"run {run.id} status -> {run.status} (+{time.time() - start:.0f}s)")
            last_status = run.status

    if run.status != "completed":
        _log(f"run {run.id} terminé en '{run.status}'")
        _log_run_diagnostics(thread_id, run)
        err = getattr(run, "last_error", None)
        raise RuntimeError(f"Run ended with status '{run.status}'. Detail: {err}")

    elapsed = round(time.time() - start, 1)
    _log(f"run {run.id} OK (+{elapsed:.0f}s)")

    # Tokens consommés par le run, si l'agent les expose (best effort). On
    # capte entrée/sortie séparément car la facturation Fabric les tarife
    # différemment (sortie 4× l'entrée — voir estimate_cu_seconds).
    tok_in = tok_out = tok_total = None
    usage = getattr(run, "usage", None)
    if usage is not None:
        def _u(attr):
            v = getattr(usage, attr, None)
            if v is None and isinstance(usage, dict):
                v = usage.get(attr)
            return v
        tok_in = _u("prompt_tokens")
        tok_out = _u("completion_tokens")
        tok_total = _u("total_tokens")
    meta = {
        "elapsed": elapsed,
        "tokens_in": tok_in,
        "tokens_out": tok_out,
        "tokens": tok_total,
    }
    _log(f"run {run.id} usage: elapsed={elapsed}s in={tok_in} out={tok_out} total={tok_total}")

    # Récupère le dernier message de l'assistant.
    msgs = _client.beta.threads.messages.list(thread_id=thread_id, order="desc", limit=10)
    for m in msgs.data:
        if m.role == "assistant":
            text = "".join(
                c.text.value for c in m.content if getattr(c, "type", None) == "text"
            ).strip()
            return (_localize(text) if text else "(empty response)"), meta
    return "(no response from the agent)", meta


# =========================================================================
# User-facing messages for fixed English strings from the Fabric platform.
#
# Note: the Fabric data agent does NOT officially support languages other than
# English. These messages are not generated by the LLM (content-safety / RAI
# filter or API errors), so they cannot be translated via instructions. We
# rewrite them here, app-side, into clearer guidance. Family B (chat refusals)
# is undocumented and may evolve: extend as logs reveal new cases (every
# unmapped case is logged).
# =========================================================================

# Family B — chat refusals (content-safety / RAI). Matched against the text of
# the assistant's response. (substring, case-insensitive)
_CANNED_MSG = {
    "there's content here i can't work with": (
        "⚠️ Your request was blocked by the data agent's content filter. "
        "Please rephrase your question in a professional manner."
    ),
    "i can't answer that question": (
        "I can't answer that question based on the available data."
    ),
    "i cannot answer that question": (
        "I can't answer that question based on the available data."
    ),
}

# Family A — documented technical errors (exceptions). Matched against the
# exception message surfaced in the app's except block.
_ERRORS_MSG = {
    "while a run": (
        "A request is already in progress. Please wait a moment or start "
        "a new conversation."
    ),
    "unauthorized": (
        "You don't have access to this data agent or its data sources. "
        "Please contact your administrator."
    ),
    "cannot find the requested item": (
        "The data agent is temporarily unavailable (unpublished or reconfigured). "
        "Please try again later."
    ),
    "configuration not found": (
        "The data agent is temporarily unavailable (unpublished or reconfigured). "
        "Please try again later."
    ),
    "artifact id": "Invalid data agent configuration. Please contact your administrator.",
    "invalid workspace id": "Invalid data agent configuration. Please contact your administrator.",
    "timeout": "The service took too long to respond. Please try again.",
}


def _localize(text: str) -> str:
    """Replace a fixed English refusal message (family B) with a clearer equivalent."""
    low = text.lower()
    for marker, msg in _CANNED_MSG.items():
        if marker in low:
            _log(f"[localize] refusal mapped: {marker!r}")
            return msg
    return text


def localize_error(message: str) -> str:
    """Map a documented technical error (family A) to friendly text. Fallback: raw message."""
    low = message.lower()
    for marker, msg in _ERRORS_MSG.items():
        if marker in low:
            return msg
    _log(f"[localize] unmapped error: {message!r}")
    return f"An error occurred while querying the data agent: {message}"
