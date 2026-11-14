"""Local web UI for the creative pipeline.

A single-page app that exists so a human can answer the approval gate in a
browser instead of a terminal. Every safety rule from the CLI is preserved:
the deterministic blocklist still runs, the audits still run, and a live run
is never auto-approved.

Run it:

    python -m src.webui          # demo mode (offline, no quota)
    ai-creative-agency           # after `pip install -e .[web]`

Then open http://127.0.0.1:8000 .

Set the env var PORT to change the port. Demo mode is the default; the UI
offers a "live" toggle that spends real quota and requires LLM_API_KEY.
"""
from __future__ import annotations

import os
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from langgraph.types import Command
from pydantic import BaseModel, Field

from src.cli import DEFAULT_RAW_INPUT, demo_deps, live_deps
from src.pipeline.graph import build_graph, new_thread_config
from src.schemas import AdCopy

_MAX_RUNS = 50
_RUNS: dict[str, dict[str, Any]] = {}

app = FastAPI(title="AI Creative Agency")


# ---------------------------------------------------------------------------
# State rendering
# ---------------------------------------------------------------------------


def _dump(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if isinstance(obj, list):
        return [_dump(item) for item in obj]
    if isinstance(obj, dict):
        return {key: _dump(value) for key, value in obj.items()}
    return str(obj)


def _render_state(run_id: str, entry: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    public = {
        "run_id": run_id,
        "mode": entry["mode"],
        "status": state.get("status"),
        "attempts": state.get("attempt"),
        "blocklist_hits": state.get("blocklist_hits") or [],
        "error": state.get("error"),
        "ad_copy": _dump(state.get("ad_copy")),
        "legal_audit": _dump(state.get("legal_result")),
        "brand_audit": _dump(state.get("brand_result")),
        "history": _dump(state.get("history") or []),
    }
    interrupts = state.get("__interrupt__") or []
    if interrupts:
        payload = getattr(interrupts[0], "value", interrupts[0])
        public["gate"] = _dump(payload)
    return public


def _slim_runs() -> dict[str, dict[str, Any]]:
    if len(_RUNS) > _MAX_RUNS:
        for key in list(_RUNS)[: len(_RUNS) - _MAX_RUNS]:
            _RUNS.pop(key, None)
    return _RUNS


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


class RunRequest(BaseModel):
    brief: str | None = Field(default=None, max_length=20_000)
    live: bool = False


class DecisionRequest(BaseModel):
    run_id: str
    decision: str = Field(pattern="^(approve|edit|reject)$")
    headline: str | None = Field(default=None, max_length=300)
    body: str | None = Field(default=None, max_length=5_000)
    cta: str | None = Field(default=None, max_length=120)


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(_PAGE)


@app.post("/api/run")
def api_run(req: RunRequest) -> dict[str, Any]:
    brief = (req.brief or "").strip() or DEFAULT_RAW_INPUT
    deps = live_deps() if req.live else demo_deps()
    graph = build_graph(deps=deps)
    config = new_thread_config(None)
    try:
        state = graph.invoke({"raw_input": brief}, config)
    except Exception as exc:  # quota/provider failure: surface, don't crash
        return {"mode": "live" if req.live else "demo", "status": "failed", "error": str(exc)}

    run_id = uuid.uuid4().hex[:12]
    entry = {"graph": graph, "config": config, "mode": "live" if req.live else "demo"}
    _RUNS[run_id] = entry
    _slim_runs()
    return _render_state(run_id, entry, state)


@app.post("/api/resume")
def api_resume(req: DecisionRequest) -> dict[str, Any]:
    entry = _RUNS.get(req.run_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="unknown run_id")

    if req.decision == "edit":
        headline = (req.headline or "").strip()
        body = (req.body or "").strip()
        cta = (req.cta or "").strip()
        if not headline or not body or not cta:
            raise HTTPException(status_code=422, detail="edit requires headline, body and cta")
        try:
            edited = AdCopy(headline=headline, body=body, cta=cta, disclosures=[])
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        payload = {"decision": "edit", "edited_copy": edited.model_dump()}
    elif req.decision == "approve":
        payload = {"decision": "approve"}
    else:
        payload = {"decision": "reject"}

    try:
        state = entry["graph"].invoke(Command(resume=payload), entry["config"])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"resume failed: {exc}") from exc
    return _render_state(req.run_id, entry, state)


@app.get("/api/runs")
def api_runs() -> list[dict[str, Any]]:
    return [
        {"run_id": run_id, "mode": entry["mode"]}
        for run_id, entry in list(_RUNS.items())
    ]


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Creative Agency</title>
<style>
  :root { --bg:#0f1216; --card:#171c23; --ink:#e8eaed; --mut:#9aa5b1; --acc:#6ee7a0; --warn:#fbbf24; --bad:#f87171; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--ink); font:16px/1.5 system-ui, "Segoe UI", sans-serif; }
  header { padding:18px 28px; border-bottom:1px solid #232a33; display:flex; align-items:baseline; gap:14px; }
  header h1 { font-size:20px; margin:0; letter-spacing:.2px; }
  header span { color:var(--mut); font-size:13px; }
  main { max-width:1080px; margin:0 auto; padding:28px; display:grid; gap:18px; }
  .card { background:var(--card); border:1px solid #232a33; border-radius:14px; padding:18px 20px; }
  label { font-size:13px; color:var(--mut); display:block; margin-bottom:6px; }
  textarea { width:100%; min-height:96px; background:#10151b; color:var(--ink); border:1px solid #2a323d; border-radius:10px; padding:10px; font:inherit; resize:vertical; }
  button { cursor:pointer; border:0; border-radius:10px; padding:10px 16px; font:600 14px system-ui; }
  .primary { background:var(--acc); color:#062014; }
  .ghost { background:#232a33; color:var(--ink); }
  .danger { background:var(--bad); color:#2b0707; }
  .row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
  .ad { border:1px solid #2a323d; border-radius:12px; padding:16px; background:#10151b; }
  .ad .headline { font-size:22px; font-weight:650; margin-bottom:8px; }
  .ad .body { color:#cdd3da; margin-bottom:12px; }
  .ad .cta { display:inline-block; background:#233044; color:#9ecbff; padding:6px 12px; border-radius:8px; font-weight:600; }
  .chips { display:flex; gap:8px; flex-wrap:wrap; margin:10px 0 0; }
  .chip { font-size:12px; border-radius:999px; padding:3px 10px; border:1px solid #2a323d; color:var(--mut); }
  .chip.ok { color:var(--acc); border-color:#1f4d2f; background:#0e2417; }
  .chip.bad { color:var(--bad); border-color:#4d1f1f; background:#241010; }
  .gate { border:1px solid #3a3320; background:#1c1a10; }
  .gate h2 { font-size:15px; margin:0 0 10px; color:var(--warn); }
  .gate input { width:100%; background:#10151b; color:var(--ink); border:1px solid #2a323d; border-radius:10px; padding:9px; margin-bottom:8px; font:inherit; }
  .history { font:12px ui-monospace, monospace; color:var(--mut); white-space:pre; overflow-x:auto; }
  .error { color:var(--bad); }
  .toggle { display:flex; align-items:center; gap:8px; font-size:13px; color:var(--mut); }
  footer { text-align:center; color:var(--mut); font-size:12px; padding:22px; border-top:1px solid #232a33; }
</style>
</head>
<body>
<header>
  <h1>AI Creative Agency</h1>
  <span>brief in &rarr; audited copy &rarr; human approval &rarr; export</span>
</header>
<main>
  <section class="card">
    <label for="brief">Client brief</label>
    <textarea id="brief" placeholder="Client: ..."></textarea>
    <div class="row" style="margin-top:12px">
      <button class="primary" id="go">Generate copy</button>
      <label class="toggle"><input type="checkbox" id="live"> live provider (spends quota)</label>
      <span id="runmode" class="chip">demo mode</span>
    </div>
  </section>

  <section class="card" id="result" hidden>
    <div class="row" style="justify-content:space-between; align-items:baseline">
      <h2 style="margin:0; font-size:16px">Result <span id="status" class="chip"></span></h2>
      <span id="meta" style="color:var(--mut); font-size:12px"></span>
    </div>
    <div class="ad" style="margin-top:12px">
      <div class="headline" id="r-headline"></div>
      <div class="body" id="r-body"></div>
      <span class="cta" id="r-cta"></span>
    </div>
    <div class="chips" id="chips"></div>
    <p id="err" class="error" hidden></p>

    <div id="gatecard" class="card gate" style="margin-top:14px" hidden>
      <h2>Your call — the model does not get the final word</h2>
      <input id="g-headline" placeholder="Headline">
      <textarea id="g-body" rows="3" placeholder="Body"></textarea>
      <input id="g-cta" placeholder="Call to action">
      <div class="row">
        <button class="primary" id="approve">Approve</button>
        <button class="ghost" id="save-edit">Edit &amp; re-audit</button>
        <button class="danger" id="reject">Reject</button>
      </div>
    </div>

    <pre class="history" id="history"></pre>
  </section>
</main>
<footer>Safety first: deterministic blocklist &bull; legal + brand audits &bull; human approval required &bull; everything logged to the ledger</footer>
<script>
const $ = (id) => document.getElementById(id);
let runId = null;
const defaultBrief = "Client: Northwind Coffee. Product: a monthly single-origin subscription. Audience: home espresso drinkers who want better beans without the fuss. Goal: drive subscriptions. Tone: warm, plain-spoken, no hype. Channels: email and instagram. Must include: 'cancel anytime'. Avoid: health claims and anything that sounds like a guarantee.";
$("brief").value = defaultBrief;

async function api(path, body) {
  const res = await fetch(path, { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body) });
  return { ok: res.ok, status: res.status, data: await res.json() };
}

function chip(text, cls) {
  const s = document.createElement("span");
  s.className = "chip " + (cls || "");
  s.textContent = text;
  return s;
}

function render(data) {
  $("result").hidden = false;
  $("status").textContent = data.status || "?";
  $("meta").textContent = (data.mode || "demo") + " mode" + (data.attempts != null ? " \u00b7 attempt " + data.attempts : "") + (data.run_id ? " \u00b7 " + data.run_id : "");
  $("runmode").textContent = (data.mode || "demo") + " mode";
  const ad = data.ad_copy || {};
  $("r-headline").textContent = ad.headline || "(no copy)";
  $("r-body").textContent = ad.body || "";
  $("r-cta").textContent = ad.cta || "";
  const chips = $("chips"); chips.innerHTML = "";
  const hits = data.blocklist_hits || [];
  chips.appendChild(chip(hits.length ? "blocklist: " + hits.join(", ") : "blocklist: clean", hits.length ? "bad" : "ok"));
  for (const [name, audit] of [["legal", data.legal_audit], ["brand", data.brand_audit]]) {
    if (!audit) continue;
    chips.appendChild(chip(name + ": " + (audit.approved ? "approved" : "REJECTED") + " \u00b7 risk " + audit.risk_score + "/10", audit.approved ? "ok" : "bad"));
  }
  const errEl = $("err");
  if (data.error) { errEl.textContent = data.error; errEl.hidden = false; } else { errEl.hidden = true; }
  $("history").textContent = (data.history || []).map(h => h.stage.padEnd(12) + " " + String(h.attempt).padStart(2) + "  " + h.status + (h.detail ? "  " + h.detail : "")).join("\\n");
  const gate = $("gatecard");
  if (data.gate) {
    gate.hidden = false;
    $("g-headline").value = ad.headline || "";
    $("g-body").value = ad.body || "";
    $("g-cta").value = ad.cta || "";
  } else {
    gate.hidden = true;
  }
  if (data.run_id) runId = data.run_id;
}

$("go").addEventListener("click", async () => {
  const brief = $("brief").value.trim();
  const live = $("live").checked;
  $("go").disabled = true; $("go").textContent = "Working\u2026";
  try {
    const r = await api("/api/run", { brief, live });
    render(r.data);
  } finally {
    $("go").disabled = false; $("go").textContent = "Generate copy";
  }
});

async function decide(decision) {
  const body = { run_id: runId, decision };
  if (decision === "edit") {
    body.headline = $("g-headline").value;
    body.body = $("g-body").value;
    body.cta = $("g-cta").value;
  }
  const r = await api("/api/resume", body);
  if (!r.ok) { $("err").textContent = r.data.detail || "Request failed"; $("err").hidden = false; return; }
  render(r.data);
}
$("approve").addEventListener("click", () => decide("approve"));
$("reject").addEventListener("click", () => decide("reject"));
$("save-edit").addEventListener("click", () => decide("edit"));
</script>
</body>
</html>
"""


def main() -> int:
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
