import dataclasses

from fastapi.testclient import TestClient

import sop.web.app as web
from tests.test_engine import MARGARET, FakeLLM, nlu


def test_chat_round_trip(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(web.agent, "llm", fake)
    client = TestClient(web.app)

    assert client.get("/").status_code == 200
    assert client.get("/api/config").json()["scenarios"]
    created = client.post("/api/sessions").json()
    assert created["state"]["phase"] == "VERIFY_ID"

    fake.queue.append(nlu(identity=MARGARET, case_hints={"status": "denied"}))
    response = client.post(f"/api/sessions/{created['session_id']}/messages", json={"text": "hello"})
    assert response.status_code == 200
    state = response.json()["state"]
    assert state["phase"] == "PROCESS_CASE" and state["active_case"]["case_id"] == "CL-2048"
    assert all("4472" not in e["detail"] for e in state["events"])  # PII values never reach the audit log


def test_unknown_session_and_empty_message(monkeypatch):
    monkeypatch.setattr(web.agent, "llm", FakeLLM())
    client = TestClient(web.app)
    assert client.post("/api/sessions/nope/messages", json={"text": "hi"}).status_code == 404
    session_id = client.post("/api/sessions").json()["session_id"]
    assert client.post(f"/api/sessions/{session_id}/messages", json={"text": ""}).status_code == 422
    assert client.post(f"/api/sessions/{session_id}/messages", json={"text": "   "}).status_code == 422


def test_passcode_protects_the_api(monkeypatch):
    monkeypatch.setattr(web, "settings", dataclasses.replace(web.settings, demo_passcode="s3cret"))
    client = TestClient(web.app)
    assert client.get("/api/config").json()["passcode_required"] is True
    assert client.post("/api/sessions").status_code == 401
    assert client.post("/api/sessions", headers={"X-Demo-Passcode": "wrong"}).status_code == 401
    assert client.post("/api/sessions", headers={"X-Demo-Passcode": "s3cret"}).status_code == 200
