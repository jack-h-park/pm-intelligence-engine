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
SQLite persistence, a FastAPI API, and a regression/evaluation harness.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# Set LLM_PROVIDER and the matching provider API key in .env.
uvicorn app.api.main:app --reload
```

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
