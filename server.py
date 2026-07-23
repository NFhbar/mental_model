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

from agent import MODEL, MentalModel
from tools import GitToolError, prepare_repo

app = FastAPI(title="Mental Model")

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

sessions: Dict[str, MentalModel] = {}
auth_tokens: Set[str] = set()


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


@app.get("/api/config")
def get_config():
    return {"auth_required": bool(APP_PASSWORD)}


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
        await client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": "ping"}],
            max_completion_tokens=16,
        )
        report["model_probe"] = {"ok": True}
    except Exception as exc:
        report["model_probe"] = {"ok": False, "error": str(exc)}
    return report


@app.post("/api/repo")
def open_repo(req: OpenRepoRequest, x_auth_token: str = Header(default="")):
    require_auth(x_auth_token)
    try:
        repo = prepare_repo(req.source)
    except GitToolError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    session_id = uuid.uuid4().hex
    sessions[session_id] = MentalModel(repo)
    name = os.path.basename(req.source.strip().rstrip("/"))
    if name.endswith(".git"):
        name = name[:-4]
    return {
        "session_id": session_id,
        "summary": repo.summary(),
        "root": repo.root,
        "name": name,
        "web_url": repo.remote_web_url(),
    }


@app.post("/api/ask")
async def ask(req: AskRequest):
    agent = sessions.get(req.session_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="unknown session")

    async def event_stream():
        try:
            async for event in agent.ask(req.question):
                yield {"event": event["kind"], "data": json.dumps(event)}
        except Exception as exc:
            yield {"event": "error", "data": json.dumps({"kind": "error", "text": str(exc)})}
        yield {"event": "done", "data": "{}"}

    return EventSourceResponse(event_stream())


@app.get("/api/commit/{session_id}/{sha}")
def commit_detail(session_id: str, sha: str):
    agent = sessions.get(session_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="unknown session")
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
