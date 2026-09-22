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

## Repository layout

```text
app/       FastAPI routes, stages, models, storage, and LLM adapters
eval/      Regression scenarios and quality rubrics
tests/     Unit and integration tests
docs/      API, architecture, and design contracts
```

## License

MIT — see [LICENSE](LICENSE).
