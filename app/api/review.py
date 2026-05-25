"""Gate 2 HTML review page — approve / revise / reject from a browser.

GET /runs/{run_id}/review  → renders an HTML page with the S4 evaluation
                             summary and three action buttons.

The page uses plain fetch() calls to POST to the existing JSON API endpoints
(/approve, /revise, /reject), so no new backend logic is needed here.
The Telegram Gate 2 notification includes a link to this page.
"""

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse

from app.api.deps import get_engine
from app.factory import PMEngine

router = APIRouter(prefix="/runs", tags=["review"])

# Persona display metadata
_PERSONA_META = {
    "explorer":   {"label": "Explorer",   "dimension": "Impact",        "color": "#0071e3"},
    "strategist": {"label": "Strategist", "dimension": "Strategic Fit", "color": "#1a7f37"},
    "builder":    {"label": "Builder",    "dimension": "Feasibility",   "color": "#bf8700"},
    "skeptic":    {"label": "Skeptic",    "dimension": "Confidence",    "color": "#cf222e"},
}

_STATUS_LABELS = {
    "waiting_approval":       ("Awaiting review", "waiting"),
    "running":                ("Running",          "running"),
    "completed":              ("Completed",        "completed"),
    "killed":                 ("Killed",           "killed"),
    "failed":                 ("Failed",           "killed"),
    "waiting_routing_review": ("Routing review",   "waiting"),
}


@router.get("/{run_id}/review", response_class=HTMLResponse)
async def review_page(
    run_id: str,
    engine: PMEngine = Depends(get_engine),
) -> str:
    run = engine.store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    # Signal title
    signal = engine.store.get_signal(run["signal_id"]) if run.get("signal_id") else None
    signal_title = signal["title"] if signal else run_id

    # S4 personas
    s4_raw = engine.store.get_stage_output(run_id, "s4")
    personas: list[dict] = []
    if s4_raw:
        s4_data = json.loads(s4_raw["output_json"])
        personas = s4_data.get("output", {}).get("personas", [])

    status = run.get("status", "unknown")
    status_label, status_class = _STATUS_LABELS.get(status, (status, "running"))
    is_actionable = status == "waiting_approval"

    persona_cards_html = _render_persona_cards(personas)
    actions_html = _render_actions(run_id, is_actionable, status_label)

    return _PAGE_TEMPLATE.format(
        run_id=run_id,
        run_id_short=run_id[:8],
        product_id=run.get("product_id", ""),
        signal_title=_esc(signal_title),
        status_label=status_label,
        status_class=status_class,
        persona_cards=persona_cards_html,
        actions_html=actions_html,
    )


# ---------------------------------------------------------------------------
# HTML renderers
# ---------------------------------------------------------------------------


def _render_persona_cards(personas: list[dict]) -> str:
    if not personas:
        return '<p style="color:#6e6e73;font-size:14px;">S4 evaluation output not available.</p>'

    cards = []
    for p in personas:
        name = p.get("persona", "")
        meta = _PERSONA_META.get(name, {"label": name.capitalize(), "dimension": "", "color": "#555"})
        score = p.get("score", 0)
        score_class = f"score-{min(max(score, 1), 5)}"
        argument = _esc(p.get("key_argument", ""))
        question = _esc(p.get("open_question", ""))

        cards.append(f"""
        <div class="persona">
            <div class="persona-header">
                <div>
                    <span class="persona-name" style="color:{meta['color']}">{meta['label']}</span>
                    <span class="persona-dim"> · {meta['dimension']}</span>
                </div>
                <span class="persona-score {score_class}">{score}/5</span>
            </div>
            <p class="persona-arg">{argument}</p>
            <p class="persona-q">❓ {question}</p>
        </div>""")

    return "\n".join(cards)


def _render_actions(run_id: str, is_actionable: bool, status_label: str) -> str:
    if not is_actionable:
        return f"""
        <div class="already-actioned">
            This run is not awaiting review.<br>
            <strong>Current status: {status_label}</strong>
        </div>"""

    return f"""
    <div class="actions" id="action-zone">
        <button class="btn-approve" onclick="doApprove()">✅ Approve</button>
        <button class="btn-revise" onclick="showReviseForm()">🔁 Revise</button>
        <button class="btn-reject" onclick="showRejectForm()">❌ Reject</button>

        <div class="form-area" id="revise-form" style="display:none">
            <label class="label" for="feedback-text">Feedback for Stage 4 re-run</label>
            <textarea id="feedback-text" placeholder="What should the agents reconsider?"></textarea>
            <button class="btn-submit" onclick="doRevise()">Submit revision</button>
        </div>

        <div class="form-area" id="reject-form" style="display:none">
            <label class="label" for="reject-reason">Reason for rejection</label>
            <textarea id="reject-reason" placeholder="Why is this run being killed?"></textarea>
            <button class="btn-submit" style="background:#cf222e" onclick="doReject()">Confirm rejection</button>
        </div>
    </div>"""


def _esc(text: str) -> str:
    """Minimal HTML escaping for user-supplied strings."""
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# Page template
# ---------------------------------------------------------------------------

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Gate 2 Review · {run_id_short}</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f7;color:#1d1d1f;padding:1.5rem;max-width:680px;margin:0 auto;line-height:1.5}}
h1{{font-size:20px;font-weight:700;margin-bottom:6px}}
.meta{{font-size:13px;color:#6e6e73;margin-bottom:1.5rem;display:flex;gap:12px;flex-wrap:wrap;align-items:center}}
.badge{{display:inline-block;padding:2px 9px;border-radius:4px;font-size:12px;font-weight:600}}
.badge.waiting{{background:#fef3cd;color:#664d03}}
.badge.completed{{background:#d1e7dd;color:#0f5132}}
.badge.killed{{background:#f8d7da;color:#842029}}
.badge.running{{background:#cfe2ff;color:#084298}}
.signal-title{{font-size:16px;font-weight:600;background:#fff;padding:14px 16px;border-radius:10px;margin-bottom:1rem;border-left:4px solid #0071e3}}
.personas{{display:grid;gap:10px;margin-bottom:1.5rem}}
.persona{{background:#fff;border-radius:10px;padding:14px 16px}}
.persona-header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}}
.persona-name{{font-weight:600;font-size:14px}}
.persona-dim{{font-size:12px;color:#6e6e73}}
.persona-score{{font-size:22px;font-weight:700}}
.score-5,.score-4{{color:#1a7f37}}
.score-3{{color:#bf8700}}
.score-2,.score-1{{color:#cf222e}}
.persona-arg{{font-size:14px;color:#3d3d3f;margin-bottom:6px}}
.persona-q{{font-size:12px;color:#6e6e73;font-style:italic}}
.actions{{display:grid;gap:10px;margin-bottom:1rem}}
button{{width:100%;padding:14px;border:none;border-radius:10px;font-size:16px;font-weight:600;cursor:pointer;transition:opacity .15s}}
button:hover{{opacity:.85}}
button:disabled{{opacity:.4;cursor:not-allowed}}
.btn-approve{{background:#1a7f37;color:#fff}}
.btn-revise{{background:#bf8700;color:#fff}}
.btn-reject{{background:#cf222e;color:#fff}}
.btn-submit{{background:#0071e3;color:#fff;margin-top:8px}}
.form-area{{background:#fff;border-radius:10px;padding:16px;margin-top:4px}}
.label{{font-size:13px;font-weight:600;margin-bottom:6px;display:block}}
textarea{{width:100%;min-height:90px;padding:10px;border:1px solid #d2d2d7;border-radius:8px;font-size:14px;font-family:inherit;resize:vertical}}
#result{{text-align:center;padding:16px;border-radius:10px;font-weight:600;font-size:15px;display:none;margin-top:1rem}}
#result.success{{background:#d1e7dd;color:#0f5132}}
#result.error{{background:#f8d7da;color:#842029}}
.already-actioned{{background:#e9ecef;border-radius:10px;padding:20px;text-align:center;color:#6e6e73;line-height:1.8}}
</style>
</head>
<body>
<h1>🧠 Gate 2 — Evaluation Review</h1>
<div class="meta">
  <span>Product: <strong>{product_id}</strong></span>
  <span>Run: <code>{run_id_short}</code></span>
  <span class="badge {status_class}">{status_label}</span>
</div>

<div class="signal-title">{signal_title}</div>

<div class="personas">
{persona_cards}
</div>

{actions_html}
<div id="result"></div>

<script>
const RUN_ID="{run_id}";
function showReviseForm(){{document.getElementById("revise-form").style.display="block";document.getElementById("reject-form").style.display="none"}}
function showRejectForm(){{document.getElementById("reject-form").style.display="block";document.getElementById("revise-form").style.display="none"}}
async function doApprove(){{await submit("/runs/"+RUN_ID+"/approve",{{}},"✅ Approved — pipeline continues to Stage 5.")}}
async function doRevise(){{
  const feedback=document.getElementById("feedback-text").value.trim();
  if(!feedback){{alert("Please enter feedback.");return}}
  await submit("/runs/"+RUN_ID+"/revise",{{feedback}},"🔁 Revision queued — Stage 4 will re-run with your feedback.")
}}
async function doReject(){{
  const reason=document.getElementById("reject-reason").value.trim();
  if(!reason){{alert("Please enter a reason.");return}}
  await submit("/runs/"+RUN_ID+"/reject",{{reason}},"❌ Run rejected and recorded.")
}}
async function submit(url,body,successMsg){{
  document.querySelectorAll("button").forEach(b=>b.disabled=true);
  try{{
    const r=await fetch(url,{{method:"POST",headers:{{"Content-Type":"application/json"}},body:JSON.stringify(body)}});
    const el=document.getElementById("result");
    el.style.display="block";
    if(r.ok){{
      el.className="success";el.textContent=successMsg;
      const az=document.getElementById("action-zone");if(az)az.style.display="none";
    }}else{{
      const d=await r.json().catch(()=>({{}}));
      el.className="error";el.textContent="Error: "+(d.detail||r.statusText);
      document.querySelectorAll("button").forEach(b=>b.disabled=false);
    }}
  }}catch(e){{
    const el=document.getElementById("result");
    el.style.display="block";el.className="error";el.textContent="Network error: "+e.message;
    document.querySelectorAll("button").forEach(b=>b.disabled=false);
  }}
}}
</script>
</body>
</html>"""
