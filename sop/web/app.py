"""Web API and the test UI.

    uv run uvicorn sop.web.app:app --reload
"""

from __future__ import annotations

import asyncio
import hmac
import json
from collections import OrderedDict
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ..config import ROOT, load_settings
from ..insurance.agent import InsuranceAgent
from ..insurance.data import FixtureRepo
from ..insurance.state import Session
from ..llm.client import build_client

STATIC = Path(__file__).parent / "static"
MAX_SESSIONS = 500  # in-memory demo store; oldest sessions are dropped first

settings = load_settings()
agent = InsuranceAgent(FixtureRepo(settings.fixtures_dir), settings, build_client(settings))
presets = json.loads((ROOT / "scenarios" / "presets.json").read_text(encoding="utf-8"))

app = FastAPI(title="Insurance Claims SOP Harness")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

sessions: OrderedDict[str, Session] = OrderedDict()
locks: dict[str, asyncio.Lock] = {}


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


def require_passcode(x_demo_passcode: str | None = Header(default=None)) -> None:
    if settings.demo_passcode and not hmac.compare_digest(x_demo_passcode or "", settings.demo_passcode):
        raise HTTPException(status_code=401, detail="Passcode required")


def get_session(session_id: str) -> Session:
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="This chat has expired. Start a new chat.")
    return session


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "llm_ready": agent.llm is not None}


@app.get("/api/config")
def config() -> dict[str, Any]:
    return {
        "provider": settings.provider,
        "model": settings.model,
        "nlu_model": settings.nlu_model,
        "llm_ready": agent.llm is not None,
        "as_of_date": settings.today().isoformat(),
        "passcode_required": bool(settings.demo_passcode),
        "scenarios": presets,
    }


@app.post("/api/sessions", dependencies=[Depends(require_passcode)])
def create_session() -> dict[str, Any]:
    session = agent.new_session()
    sessions[session.id] = session
    while len(sessions) > MAX_SESSIONS:
        old_id, _ = sessions.popitem(last=False)
        locks.pop(old_id, None)
    return {"session_id": session.id, "transcript": session.transcript, "state": agent.snapshot(session)}


@app.get("/api/sessions/{session_id}", dependencies=[Depends(require_passcode)])
def read_session(session_id: str) -> dict[str, Any]:
    session = get_session(session_id)
    return {"session_id": session.id, "transcript": session.transcript, "state": agent.snapshot(session)}


@app.post("/api/sessions/{session_id}/messages", dependencies=[Depends(require_passcode)])
async def send_message(session_id: str, body: MessageIn) -> dict[str, Any]:
    session = get_session(session_id)
    if agent.llm is None:
        raise HTTPException(status_code=503, detail="No model configured. Set LLM_API_KEY and restart the server.")
    lock = locks.setdefault(session_id, asyncio.Lock())
    async with lock:
        # The model SDKs are blocking; run the turn in a worker thread so other chats stay responsive.
        reply = await run_in_threadpool(agent.handle, session, body.text)
    return {"reply": reply, "state": agent.snapshot(session)}
