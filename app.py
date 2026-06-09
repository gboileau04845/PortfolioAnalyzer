"""App de chat FastAPI relayant les questions vers un Fabric Data Agent.

Le navigateur ne parle jamais directement au data agent : il appelle /api/chat,
et le backend (qui détient le secret SPN) relaie vers l'agent. Une conversation
= un thread Fabric, conservé en mémoire par session.

L'app estime aussi la consommation CU de l'agent (taux officiel Fabric, voir
fabric_agent.estimate_cu_seconds) et la suit par utilisateur + au total, contre
un budget exprimé en % d'une capacité (configuré dans .env).
"""

import datetime
import json
import os
import uuid

from dotenv import load_dotenv

# .env chargé AVANT d'importer fabric_agent (qui lit les variables à l'import,
# et instancie le client OpenAI + le credential SPN au niveau module).
load_dotenv(override=True)

from fastapi import FastAPI, Request, Body, Header  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402

import fabric_agent  # noqa: E402

app = FastAPI(title="Fabric Data Agent — Chat")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# session_id (cookie/navigateur) -> thread_id Fabric. En mémoire processus
# (suffisant pour un POC ; perdu au redémarrage).
_sessions: dict[str, str] = {}
# session_id -> {"upn", "name"} : à qui attribuer la consommation.
_session_user: dict[str, dict] = {}
# upn -> {"name", "day", "cu_seconds", "queries", "tokens"} : conso du jour.
# Persisté sur disque pour survivre à un redémarrage de l'app (cf. _load/_save).
_usage: dict[str, dict] = {}
_USAGE_STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage_store.json")
# upn -> liste de messages [{"role", "text", "cu"?}] : historique de conversation,
# persisté pour survivre à une déconnexion / reconnexion / redémarrage.
_history: dict[str, list] = {}
_CONV_STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "conversations_store.json")
_HISTORY_MAX = 400  # messages conservés par utilisateur (garde-fou)

# --- Budgets CU : exprimés en % d'une capacité Fabric (SKU) sur une fenêtre ---
# Deux plafonds indépendants :
#   - GROUPE : part max pour tout l'agent (total entreprise).
#   - USER   : part max par utilisateur.
# Un utilisateur est bloqué dès que l'UN OU L'AUTRE est atteint.
# Valeurs par défaut depuis .env, surchargeables à chaud via la page
# Administration (persistées dans admin_config.json).
_ADMIN_STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin_config.json")
# Code d'accès à la page Administration (vide = pas de protection).
ADMIN_CODE = os.environ.get("ADMIN_ACCESS_CODE", "").strip()
_env_group = float(
    os.environ.get("AGENT_CU_BUDGET_GROUP_PCT")
    or os.environ.get("AGENT_CU_BUDGET_PCT")
    or "100"
)
_cfg = {
    "sku_cu": float(os.environ.get("FABRIC_CAPACITY_SKU_CU", "64")),
    "window_hours": float(os.environ.get("AGENT_CU_BUDGET_WINDOW_HOURS", "24")),
    "group_pct": _env_group,
    "user_pct": float(os.environ.get("AGENT_CU_BUDGET_USER_PCT") or _env_group),
}


def _load_admin_config() -> None:
    """Surcharge _cfg avec les réglages admin persistés (s'ils existent)."""
    try:
        with open(_ADMIN_STORE, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return
    for k in ("sku_cu", "window_hours", "group_pct", "user_pct"):
        if isinstance(data, dict) and data.get(k) is not None:
            try:
                _cfg[k] = float(data[k])
            except (TypeError, ValueError):
                pass


def _save_admin_config() -> None:
    try:
        tmp = _ADMIN_STORE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_cfg, f)
        os.replace(tmp, _ADMIN_STORE)
    except OSError:
        pass


def _cap_cu_seconds() -> float:
    return _cfg["sku_cu"] * _cfg["window_hours"] * 3600.0


def _group_budget() -> float:
    return _cap_cu_seconds() * (_cfg["group_pct"] / 100.0)


def _user_budget() -> float:
    return _cap_cu_seconds() * (_cfg["user_pct"] / 100.0)


_load_admin_config()


def _today() -> str:
    return datetime.date.today().isoformat()


def _load_usage() -> None:
    """Recharge la conso depuis le disque, en ne gardant QUE le jour courant.

    Permet à l'historique du jour de survivre à un redémarrage de l'app. Les
    entrées d'un jour antérieur sont écartées (remise à zéro quotidienne).
    """
    try:
        with open(_USAGE_STORE, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return
    today = _today()
    for upn, e in (data or {}).items():
        if isinstance(e, dict) and e.get("day") == today:
            _usage[upn] = {
                "name": e.get("name") or upn, "day": today,
                "cu_seconds": float(e.get("cu_seconds") or 0.0),
                "queries": int(e.get("queries") or 0),
                "tokens": int(e.get("tokens") or 0),
            }


def _save_usage() -> None:
    """Écrit la conso courante sur disque (best effort, écriture atomique)."""
    try:
        tmp = _USAGE_STORE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_usage, f)
        os.replace(tmp, _USAGE_STORE)
    except OSError:
        pass  # best effort : ne jamais casser une requête pour la persistance


def _load_conversations() -> None:
    """Recharge l'historique des conversations (par utilisateur) depuis le disque."""
    try:
        with open(_CONV_STORE, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return
    if isinstance(data, dict):
        for upn, msgs in data.items():
            if isinstance(msgs, list):
                _history[upn] = msgs[-_HISTORY_MAX:]


def _save_conversations() -> None:
    try:
        tmp = _CONV_STORE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_history, f)
        os.replace(tmp, _CONV_STORE)
    except OSError:
        pass


_load_usage()  # restaure l'historique du jour au démarrage
_load_conversations()  # restaure l'historique des conversations au démarrage


def _user_entry(upn: str, name: str | None) -> dict:
    """Renvoie (en le créant/réinitialisant au besoin) le compteur du jour."""
    today = _today()
    e = _usage.get(upn)
    if e is None or e["day"] != today:  # nouveau jour -> remise à zéro
        e = {"name": name or upn, "day": today, "cu_seconds": 0.0, "queries": 0, "tokens": 0}
        _usage[upn] = e
    if name:
        e["name"] = name
    return e


def _pct(cu: float, budget: float) -> float:
    return round(100.0 * cu / budget, 2) if budget else 0.0


def _snapshot() -> dict:
    """État courant : budgets, total entreprise (vs groupe), détail par user (vs user)."""
    today = _today()
    grp_budget, usr_budget = _group_budget(), _user_budget()
    users, total_cu, total_q, total_tok = [], 0.0, 0, 0
    for upn, e in _usage.items():
        if e["day"] != today:
            continue
        total_cu += e["cu_seconds"]
        total_q += e["queries"]
        total_tok += e["tokens"]
        users.append({
            "user": e["name"], "upn": upn,
            "cu_seconds": round(e["cu_seconds"], 1),
            "pct": _pct(e["cu_seconds"], usr_budget),  # vs plafond user
            "queries": e["queries"], "tokens": e["tokens"],
        })
    users.sort(key=lambda u: u["cu_seconds"], reverse=True)
    return {
        "budget": {
            "sku_cu": _cfg["sku_cu"], "window_hours": _cfg["window_hours"],
            "group_pct": _cfg["group_pct"], "user_pct": _cfg["user_pct"],
            "group_cu_seconds": round(grp_budget, 1),
            "user_cu_seconds": round(usr_budget, 1),
        },
        "total": {  # total entreprise mesuré vs le plafond GROUPE
            "cu_seconds": round(total_cu, 1),
            "pct": _pct(total_cu, grp_budget),
            "queries": total_q, "tokens": total_tok,
        },
        "users": users,
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {})


@app.post("/api/login")
def login(payload: dict = Body(default={})):
    """Déclenche la connexion Entra ID (ouvre le navigateur) AVANT le chat."""
    try:
        user = fabric_agent.login()
        sid = (payload or {}).get("session_id")
        upn = (user or {}).get("upn") or (user or {}).get("name") or "unknown"
        if sid:
            _session_user[sid] = {"upn": upn, "name": (user or {}).get("name") or upn}
            _user_entry(upn, (user or {}).get("name"))
            _save_usage()
        return {
            "ok": True, "user": user, "usage": _snapshot(),
            "history": _history.get(upn, []),
        }
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=502
        )


@app.get("/api/usage")
def usage():
    """Instantané de la consommation CU (pour rafraîchir les jauges)."""
    return _snapshot()


@app.post("/api/chat")
def chat(payload: dict = Body(...)):
    session_id = payload.get("session_id") or str(uuid.uuid4())
    question = (payload.get("question") or "").strip()
    if not question:
        return JSONResponse({"error": "Empty question."}, status_code=400)

    # Garde-fou : on BLOQUE la requête AVANT tout appel à l'agent (donc aucune CU
    # consommée) si le plafond GROUPE (total entreprise) OU le plafond de
    # l'UTILISATEUR courant est atteint. Les budgets se réinitialisent chaque jour.
    snap = _snapshot()
    who = _session_user.get(session_id) or {"upn": "unknown", "name": "Unknown user"}
    user_cu = _user_entry(who["upn"], who["name"])["cu_seconds"]

    grp_budget, usr_budget = _group_budget(), _user_budget()
    blocked_msg = None
    if grp_budget and snap["total"]["cu_seconds"] >= grp_budget:
        blocked_msg = (
            f"Group CU budget reached ({_cfg['group_pct']:g}% of the Fabric capacity). "
            "The agent is paused for everyone to protect the capacity. "
            "It resets daily; raise the group budget in Administration to allow more."
        )
    elif usr_budget and user_cu >= usr_budget:
        blocked_msg = (
            f"Your personal CU budget is reached ({_cfg['user_pct']:g}% of the Fabric "
            "capacity). Your access is paused; it resets daily. Raise the per-user "
            "budget in Administration to allow more."
        )
    if blocked_msg:
        return JSONResponse(
            {"session_id": session_id, "error": blocked_msg, "blocked": True, "usage": snap},
            status_code=429,
        )

    try:
        thread_id = _sessions.get(session_id)
        if not thread_id:
            thread_id = fabric_agent.new_thread()
            _sessions[session_id] = thread_id
        answer, meta = fabric_agent.ask(thread_id, question)

        # Estimation CU de la question + imputation à l'utilisateur de la session.
        cu, basis = fabric_agent.estimate_cu_seconds(
            tokens_in=meta.get("tokens_in"),
            tokens_out=meta.get("tokens_out"),
            tokens_total=meta.get("tokens"),
            question=question,
            answer=answer,
        )
        who = _session_user.get(session_id) or {"upn": "unknown", "name": "Unknown user"}
        e = _user_entry(who["upn"], who["name"])
        e["cu_seconds"] += cu
        e["queries"] += 1
        e["tokens"] += meta.get("tokens") or 0
        _save_usage()  # persiste la conso (survit au redémarrage de l'app)
        meta = {**meta, "cu_seconds": cu, "cu_basis": basis}

        # Persiste l'échange dans l'historique de l'utilisateur (pour le restaurer
        # après déconnexion / reconnexion / redémarrage).
        hist = _history.setdefault(who["upn"], [])
        hist.append({"role": "user", "text": question})
        hist.append({"role": "agent", "text": answer, "cu": cu})
        del hist[:-_HISTORY_MAX]
        _save_conversations()

        return {
            "session_id": session_id, "answer": answer,
            "meta": meta, "usage": _snapshot(),
        }
    except Exception as e:  # remonté proprement au front, pas un 500 brut
        return JSONResponse(
            {"session_id": session_id, "error": fabric_agent.localize_error(str(e))},
            status_code=502,
        )


def _check_admin_code(code: str | None) -> bool:
    """True si l'accès admin est autorisé (code correct, ou aucun code requis)."""
    return (not ADMIN_CODE) or ((code or "").strip() == ADMIN_CODE)


_FORBIDDEN = JSONResponse({"ok": False, "error": "Invalid admin code."}, status_code=403)


@app.get("/api/admin/config")
def admin_get_config(x_admin_code: str | None = Header(default=None)):
    """Réglages de budget courants + instantané de conso (page Administration)."""
    if not _check_admin_code(x_admin_code):
        return _FORBIDDEN
    return {"ok": True, "config": dict(_cfg), "usage": _snapshot()}


@app.post("/api/admin/config")
def admin_set_config(payload: dict = Body(...), x_admin_code: str | None = Header(default=None)):
    """Met à jour les budgets à chaud (et persiste dans admin_config.json)."""
    if not _check_admin_code(x_admin_code):
        return _FORBIDDEN
    errors = []
    new = dict(_cfg)
    for key, lo, hi in (
        ("sku_cu", 0.0, 100000.0),
        ("window_hours", 0.1, 8760.0),
        ("group_pct", 0.0, 100.0),
        ("user_pct", 0.0, 100.0),
    ):
        if key in payload and payload[key] is not None and payload[key] != "":
            try:
                v = float(payload[key])
            except (TypeError, ValueError):
                errors.append(f"{key}: not a number")
                continue
            if not (lo <= v <= hi):
                errors.append(f"{key}: out of range [{lo:g}, {hi:g}]")
                continue
            new[key] = v
    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=400)
    _cfg.update(new)
    _save_admin_config()
    return {"ok": True, "config": dict(_cfg), "usage": _snapshot()}


@app.post("/api/reset")
def reset(payload: dict = Body(...)):
    session_id = payload.get("session_id")
    thread_id = _sessions.pop(session_id, None)
    if thread_id:
        fabric_agent.delete_thread(thread_id)
    return {"ok": True}


@app.post("/api/logout")
def logout(payload: dict = Body(default={})):
    """Déconnexion réelle : nettoie la session puis vide le jeton mis en cache
    (le prochain Sign in laissera choisir un autre compte)."""
    sid = (payload or {}).get("session_id")
    if sid:
        thread_id = _sessions.pop(sid, None)
        if thread_id:
            fabric_agent.delete_thread(thread_id)
        _session_user.pop(sid, None)
    fabric_agent.logout()
    return {"ok": True}
