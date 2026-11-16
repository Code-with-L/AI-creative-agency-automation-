"""Tests for the local web UI (demo mode only; no provider, no quota)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from src.webui import app

client = TestClient(app)

BRIEF = (
    "Client: Northwind Coffee. Product: a monthly single-origin subscription. "
    "Audience: home espresso drinkers. Goal: drive subscriptions. "
    "Tone: warm, plain-spoken. Must include: 'cancel anytime'."
)


def test_health_is_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_index_serves_page():
    response = client.get("/")
    assert response.status_code == 200
    assert "AI Creative Agency" in response.text
    assert "Generate copy" in response.text


def test_run_reaches_the_human_gate():
    response = client.post("/api/run", json={"brief": BRIEF})
    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "demo"
    assert data["status"] == "awaiting_human"
    assert data["gate"] is not None
    assert data["ad_copy"]["body"]
    assert data["blocklist_hits"] == []
    assert data["legal_audit"]["approved"] is True


def test_approve_finishes_the_run():
    response = client.post("/api/run", json={"brief": BRIEF})
    run_id = response.json()["run_id"]
    finished = client.post("/api/resume", json={"run_id": run_id, "decision": "approve"})
    assert finished.status_code == 200
    assert finished.json()["status"] == "approved"


def test_edit_replaces_copy_then_approve():
    response = client.post("/api/run", json={"brief": BRIEF})
    run_id = response.json()["run_id"]
    edited = client.post(
        "/api/resume",
        json={
            "run_id": run_id,
            "decision": "edit",
            "headline": "Better beans, better mornings.",
            "body": "Single-origin bags delivered monthly. Cancel anytime.",
            "cta": "Get started",
        },
    )
    assert edited.status_code == 200
    data = edited.json()
    assert data.get("gate") is not None or data["status"] == "approved"

    final = client.post("/api/resume", json={"run_id": run_id, "decision": "approve"})
    assert final.json()["status"] == "approved"
    assert final.json()["ad_copy"]["body"] == "Single-origin bags delivered monthly. Cancel anytime."


def test_edit_requires_all_fields():
    response = client.post("/api/run", json={"brief": BRIEF})
    run_id = response.json()["run_id"]
    incomplete = client.post(
        "/api/resume",
        json={"run_id": run_id, "decision": "edit", "headline": "Only headline"},
    )
    assert incomplete.status_code == 422


def test_reject_marks_rejected():
    response = client.post("/api/run", json={"brief": BRIEF})
    run_id = response.json()["run_id"]
    rejected = client.post("/api/resume", json={"run_id": run_id, "decision": "reject"})
    assert rejected.json()["status"] == "rejected"


def test_unknown_run_id_is_404():
    response = client.post("/api/resume", json={"run_id": "nope", "decision": "approve"})
    assert response.status_code == 404
