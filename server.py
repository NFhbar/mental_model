import asyncio
import json
import os
import secrets
import uuid
from typing import Dict, Set

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from agent import MODEL, REASONING_EFFORT, MentalModel
from tools import GitToolError, prepare_repo

app = FastAPI(title="Mental Model")

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
MAX_REPOSITORIES = 3

sessions: Dict[str, MentalModel] = {}
session_owners: Dict[str, str] = {}
session_info: Dict[str, Dict] = {}
auth_tokens: Set[str] = set()


@app.on_event("shutdown")
def cleanup_temporary_repositories():
    for agent in sessions.values():
        agent.repo.cleanup()


class OpenRepoRequest(BaseModel):
    source: str


class AskRequest(BaseModel):
    session_id: str
    question: str


class AuthRequest(BaseModel):
    password: str


def require_auth(token: str):
    if APP_PASSWORD and token not in auth_tokens:
        raise HTTPException(status_code=401, detail="authentication required")


def owner_id(token: str) -> str:
    return token if APP_PASSWORD else "public"


def normalized_source(source: str) -> str:
    source = source.strip()
    if source.startswith(("http://", "https://", "git@")):
        return source.rstrip("/").removesuffix(".git").lower()
    return os.path.realpath(os.path.expanduser(source))


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
        "authenticated": not APP_PASSWORD or x_auth_token in auth_tokens,
        "max_repositories": MAX_REPOSITORIES,
    }


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/auth")
def authenticate(req: AuthRequest):
    if not APP_PASSWORD:
        return {"token": ""}
    if not secrets.compare_digest(req.password, APP_PASSWORD):
        raise HTTPException(status_code=401, detail="wrong password")
    token = uuid.uuid4().hex
    auth_tokens.add(token)
    return {"token": token}


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
    source_key = normalized_source(req.source)
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
    try:
        repo = prepare_repo(req.source)
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
        "root": repo.root,
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
    agent.repo.cleanup()
    del sessions[session_id]
    session_owners.pop(session_id, None)
    session_info.pop(session_id, None)
    return {"deleted": True}


@app.get("/api/meta/{session_id}")
def session_meta(session_id: str, x_auth_token: str = Header(default="")):
    agent = owned_agent(session_id, x_auth_token)
    return agent.metadata()


@app.post("/api/ask")
async def ask(req: AskRequest, x_auth_token: str = Header(default="")):
    agent = owned_agent(req.session_id, x_auth_token)
    snapshot = agent.snapshot_state()

    async def event_stream():
        completed = False
        try:
            async for event in agent.ask(req.question):
                if event["kind"] == "answer":
                    completed = True
                yield {"event": event["kind"], "data": json.dumps(event)}
        except asyncio.CancelledError:
            if not completed:
                agent.restore_state(snapshot)
            raise
        except Exception as exc:
            agent.restore_state(snapshot)
            yield {"event": "error", "data": json.dumps({"kind": "error", "text": str(exc)})}
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
