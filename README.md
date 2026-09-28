# PM Intelligence Engine

A provider-neutral Python service that turns incoming signals into a
traceable, human-gated decision workflow. It is designed to work with
operator-supplied context and integrations; no private context or credentials
are included in this repository.

## Workflow

`SENSE → DECIDE → LEARN`

- **SENSE** normalizes an incoming signal.
- **DECIDE** runs seven typed stages, including four independent evaluations
  and explicit human approval gates.
- **LEARN** persists the run and exports structured artifacts for downstream
  systems.

The engine supports Claude and OpenAI through the `LLMProvider` protocol,
SQLite persistence, a FastAPI API, and a regression/evaluation harness. With
`LLM_PROVIDER=openai`, retryable GPT-6 Sol failures fall back to Claude Sonnet 5;
retryable GPT-6 Luna failures fall back to Claude Haiku 4.5. Other model IDs and
non-retryable errors do not switch providers. Stage metadata records the model
that actually returned the answer.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,openai]"
cp .env.example .env
# Set OPENAI_API_KEY and ANTHROPIC_API_KEY for the OpenAI fallback chain.
uvicorn app.api.main:app --reload
```

The `openai` extra installs both provider SDKs because the OpenAI provider uses
the Anthropic SDK for its same-tier retryable-failure fallback. On a deployed
service, install `.[openai]` into the same Python environment selected by its
service definition before restarting it with `make service-restart`.

All authenticated endpoints require `PM_PLATFORM_API_TOKEN`; `/health` is the
only unauthenticated endpoint. The default context and wiki roots are local
directories. Provide your own context files or override the paths in `.env`.

## Development

```bash
pytest -q
ruff check app tests eval
mypy --strict app
python eval/runner.py
```

The evaluation fixtures are examples only. Replace them with domain-appropriate
fixtures before using the engine for production decisions. Do not commit API
keys, generated databases, run archives, or private context repositories.

## Ambiguous S2K job recovery

S2K workers persist `inference_state=started` and `inference_started_at` before
external inference. A failure or expired lease after that boundary fences the
job as `state=exhausted`, `inference_state=terminal_unknown`; it never queues
another call. Completed analysis changes the inference state to `complete`.
Expired historical scoped-candidate jobs are fenced conservatively even when
their older payload lacks the marker. Generic pre-call leases retain recovery.
These fields live in the existing job JSON, requiring no schema migration.

After verifying the original worker/bridge process is gone and its lease has
expired, use authenticated `POST /insight-jobs/{job_id}/reconcile-unknown` with
a JSON `reason` and `X-S2K-Reconciliation-Token`. This uses the existing dedicated
`S2K_RECONCILIATION_TOKEN` and server-owned `S2K_RECONCILIATION_OPERATOR_ID`.
The endpoint rejects active leases and completed jobs and records the original
operator, reason, prior state, and time in the job. Repeating reconciliation
preserves that first record. It does not replay inference, create an Insight,
send a notification, release a reservation, or assert zero provider usage.
The Engine checks lease/state eligibility; checking process liveness is the
operator's prerequisite. Preserve uncertain billing and source evidence for review.

## Repository layout

```text
app/       FastAPI routes, stages, models, storage, and LLM adapters
eval/      Regression scenarios and quality rubrics
tests/     Unit and integration tests
docs/      API, architecture, and design contracts
```

## License

MIT — see [LICENSE](LICENSE).

### S2K worker lease and enclosing deadlines

S2K workers size their job lease from the bridge instance's enforced completion
bound. Analysis and knowledge judgment each allow an initial completion and two
JSON repairs: with a 210-second completion bound, the lease is
`2 * 3 * 210 + 60 = 1320` seconds. The extra minute covers context preparation and
saving. Generic, scoped Candidate, and evidence-backfill workers use this same
calculation; other providers and direct storage claims keep the 120-second default.
An expired lease after inference starts remains ambiguous and is fenced from
automatic replay. This change grants neither new calls nor spending allowance.

A longer lease does not extend an HTTP caller or the Hermes scheduler. The installed
supplier's 330-second triage HTTP limit, 900-second supplier process limit,
600-second worker HTTP limit, and ops profile's 900-second cron script limit
must be reconciled before declaring the full intake deadline chain ready for
cutover. No scheduler or caller change is included here.
