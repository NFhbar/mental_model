import asyncio
import json
import logging
import os
import secrets
import time
import uuid
from collections import defaultdict, deque
from typing import Deque, Dict

from config import load_local_env

load_local_env()

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from agent import MODEL, REASONING_EFFORT, MentalModel
from tools import GitToolError, canonical_repo_source, prepare_repo

app = FastAPI(title="Mental Model")
logger = logging.getLogger("mental-model")
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=[
        host.strip()
        for host in os.environ.get(
            "ALLOWED_HOSTS",
            "localhost,127.0.0.1,testserver,*.onrender.com",
        ).split(",")
        if host.strip()
    ],
)

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
MAX_REPOSITORIES = 3
MAX_TOTAL_REPOSITORIES = int(os.environ.get("MAX_TOTAL_REPOSITORIES", "9"))
AUTH_TOKEN_TTL_SECONDS = int(os.environ.get("AUTH_TOKEN_TTL_SECONDS", "28800"))
AUTH_FAILURE_LIMIT = int(os.environ.get("AUTH_FAILURE_LIMIT", "5"))
AUTH_FAILURE_WINDOW_SECONDS = int(
    os.environ.get("AUTH_FAILURE_WINDOW_SECONDS", "300")
)
ASK_RATE_LIMIT = int(os.environ.get("ASK_RATE_LIMIT", "20"))
ASK_RATE_WINDOW_SECONDS = int(os.environ.get("ASK_RATE_WINDOW_SECONDS", "60"))
CLONE_TIMEOUT_SECONDS = int(os.environ.get("GIT_CLONE_TIMEOUT_SECONDS", "120"))
ALLOW_LOCAL_REPOS = os.environ.get("ALLOW_LOCAL_REPOS", "true").lower() in {
    "1",
    "true",
    "yes",
}

sessions: Dict[str, MentalModel] = {}
session_owners: Dict[str, str] = {}
session_info: Dict[str, Dict] = {}
session_locks: Dict[str, asyncio.Lock] = {}
auth_tokens: Dict[str, float] = {}
auth_failures: Dict[str, Deque[float]] = defaultdict(deque)
ask_requests: Dict[str, Deque[float]] = defaultdict(deque)


@app.on_event("shutdown")
def cleanup_temporary_repositories():
    for agent in sessions.values():
        agent.repo.cleanup()


class OpenRepoRequest(BaseModel):
    source: str = Field(min_length=1, max_length=2048)


class AskRequest(BaseModel):
    session_id: str = Field(min_length=16, max_length=64)
    question: str = Field(min_length=1, max_length=4000)


class AuthRequest(BaseModel):
    password: str = Field(max_length=512)


def prune_requests(requests: Deque[float], window_seconds: int):
    cutoff = time.monotonic() - window_seconds
    while requests and requests[0] <= cutoff:
        requests.popleft()


def enforce_rate_limit(
    buckets: Dict[str, Deque[float]],
    key: str,
    limit: int,
    window_seconds: int,
    detail: str,
):
    requests = buckets.setdefault(key, deque())
    prune_requests(requests, window_seconds)
    if len(requests) >= limit:
        retry_after = max(1, int(window_seconds - (time.monotonic() - requests[0])))
        raise HTTPException(
            status_code=429,
            detail=detail,
            headers={"Retry-After": str(retry_after)},
        )
    requests.append(time.monotonic())


def token_is_valid(token: str) -> bool:
    if not APP_PASSWORD:
        return True
    expires_at = auth_tokens.get(token)
    if expires_at is None or expires_at <= time.monotonic():
        auth_tokens.pop(token, None)
        return False
    return True


def require_auth(token: str):
    if not APP_PASSWORD:
        return
    if not token_is_valid(token):
        raise HTTPException(status_code=401, detail="authentication required")


def owner_id(token: str) -> str:
    return token if APP_PASSWORD else "public"


def owned_agent(session_id: str, token: str) -> MentalModel:
    require_auth(token)
    agent = sessions.get(session_id)
    if agent is None or session_owners.get(session_id) != owner_id(token):
        raise HTTPException(status_code=404, detail="unknown session")
    return agent


@app.get("/api/config")
def get_config(x_auth_token: str = Header(default="")):
    return {
        "auth_required": bool(APP_PASSWORD),
        "authenticated": token_is_valid(x_auth_token),
        "max_repositories": MAX_REPOSITORIES,
        "allow_local_repositories": ALLOW_LOCAL_REPOS,
    }


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/auth")
def authenticate(req: AuthRequest, request: Request):
    if not APP_PASSWORD:
        return {"token": ""}
    client = request.client.host if request.client else "unknown"
    failures = auth_failures[client]
    prune_requests(failures, AUTH_FAILURE_WINDOW_SECONDS)
    if len(failures) >= AUTH_FAILURE_LIMIT:
        retry_after = max(
            1,
            int(
                AUTH_FAILURE_WINDOW_SECONDS
                - (time.monotonic() - failures[0])
            ),
        )
        raise HTTPException(
            status_code=429,
            detail="too many authentication failures",
            headers={"Retry-After": str(retry_after)},
        )
    if not secrets.compare_digest(req.password, APP_PASSWORD):
        failures.append(time.monotonic())
        raise HTTPException(status_code=401, detail="wrong password")
    auth_failures.pop(client, None)
    token = secrets.token_urlsafe(32)
    auth_tokens[token] = time.monotonic() + AUTH_TOKEN_TTL_SECONDS
    return {"token": token, "expires_in": AUTH_TOKEN_TTL_SECONDS}


@app.get("/api/diagnose")
async def diagnose(x_auth_token: str = Header(default="")):
    require_auth(x_auth_token)
    key = os.environ.get("OPENAI_API_KEY", "")
    report = {
        "api_key_set": bool(key),
        "api_key_suffix": key[-4:] if key else None,
        "configured_model": MODEL,
        "api": "responses",
        "reasoning_effort": REASONING_EFFORT,
        "available_models": [],
        "model_probe": None,
    }
    if not key:
        report["model_probe"] = {"ok": False, "error": "OPENAI_API_KEY is not set"}
        return report
    client = AsyncOpenAI()
    try:
        page = await client.models.list()
        report["available_models"] = sorted(m.id for m in page.data)
    except Exception as exc:
        report["available_models_error"] = str(exc)
    try:
        response = await client.responses.create(
            model=MODEL,
            input="Call diagnostic_ping.",
            tools=[
                {
                    "type": "function",
                    "name": "diagnostic_ping",
                    "description": "Confirm that function tools are available.",
                    "parameters": {"type": "object", "properties": {}},
                    "strict": False,
                }
            ],
            reasoning={"effort": REASONING_EFFORT},
            max_output_tokens=64,
            store=False,
        )
        tool_ok = any(item.type == "function_call" for item in response.output)
        report["model_probe"] = {
            "ok": tool_ok,
            "error": None if tool_ok else "model did not call the diagnostic tool",
        }
    except Exception as exc:
        report["model_probe"] = {"ok": False, "error": str(exc)}
    return report


@app.post("/api/repo")
def open_repo(req: OpenRepoRequest, x_auth_token: str = Header(default="")):
    require_auth(x_auth_token)
    owner = owner_id(x_auth_token)
    try:
        source_key = canonical_repo_source(req.source, ALLOW_LOCAL_REPOS)
    except GitToolError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    for session_id, info in session_info.items():
        if session_owners.get(session_id) == owner and info["source_key"] == source_key:
            return info["response"]
    owned_sessions = [
        session_id
        for session_id, session_owner in session_owners.items()
        if session_owner == owner
    ]
    if len(owned_sessions) >= MAX_REPOSITORIES:
        raise HTTPException(
            status_code=409,
            detail=f"repository limit reached ({MAX_REPOSITORIES})",
        )
    if len(sessions) >= MAX_TOTAL_REPOSITORIES:
        raise HTTPException(
            status_code=503,
            detail="global repository capacity reached",
        )
    try:
        repo = prepare_repo(
            source_key,
            allow_local_repos=ALLOW_LOCAL_REPOS,
            clone_timeout_seconds=CLONE_TIMEOUT_SECONDS,
        )
    except GitToolError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    session_id = uuid.uuid4().hex
    sessions[session_id] = MentalModel(repo)
    session_owners[session_id] = owner
    name = os.path.basename(req.source.strip().rstrip("/"))
    if name.endswith(".git"):
        name = name[:-4]
    response = {
        "session_id": session_id,
        "source": req.source.strip(),
        "summary": repo.summary(),
        "name": name,
        "web_url": repo.remote_web_url(),
    }
    session_info[session_id] = {
        "source_key": source_key,
        "response": response,
    }
    return response


@app.delete("/api/repo/{session_id}")
def close_repo(session_id: str, x_auth_token: str = Header(default="")):
    agent = owned_agent(session_id, x_auth_token)
    lock = session_locks.get(session_id)
    if lock and lock.locked():
        raise HTTPException(status_code=409, detail="investigation in progress")
    agent.repo.cleanup()
    del sessions[session_id]
    session_owners.pop(session_id, None)
    session_info.pop(session_id, None)
    session_locks.pop(session_id, None)
    return {"deleted": True}


@app.get("/api/meta/{session_id}")
def session_meta(session_id: str, x_auth_token: str = Header(default="")):
    agent = owned_agent(session_id, x_auth_token)
    return agent.metadata()


@app.post("/api/ask")
async def ask(req: AskRequest, x_auth_token: str = Header(default="")):
    agent = owned_agent(req.session_id, x_auth_token)
    enforce_rate_limit(
        ask_requests,
        owner_id(x_auth_token),
        ASK_RATE_LIMIT,
        ASK_RATE_WINDOW_SECONDS,
        "investigation rate limit reached",
    )
    lock = session_locks.setdefault(req.session_id, asyncio.Lock())

    async def event_stream():
        async with lock:
            snapshot = agent.snapshot_state()
            completed = False
            try:
                async for event in agent.ask(req.question):
                    if event["kind"] == "answer":
                        completed = True
                    elif event["kind"] == "error":
                        agent.restore_state(snapshot)
                    yield {"event": event["kind"], "data": json.dumps(event)}
            except asyncio.CancelledError:
                if not completed:
                    agent.restore_state(snapshot)
                raise
            except Exception:
                agent.restore_state(snapshot)
                logger.exception("investigation failed")
                yield {
                    "event": "error",
                    "data": json.dumps(
                        {"kind": "error", "text": "investigation failed"}
                    ),
                }
            yield {"event": "done", "data": "{}"}

    return EventSourceResponse(event_stream())


@app.get("/api/commit/{session_id}/{sha}")
def commit_detail(
    session_id: str, sha: str, x_auth_token: str = Header(default="")
):
    agent = owned_agent(session_id, x_auth_token)
    try:
        return {"detail": agent.repo.show_commit(sha)}
    except GitToolError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


if os.path.isdir(os.path.join(os.path.dirname(__file__), "frontend", "dist")):
    app.mount(
        "/",
        StaticFiles(
            directory=os.path.join(os.path.dirname(__file__), "frontend", "dist"),
            html=True,
        ),
        name="frontend",
    )
