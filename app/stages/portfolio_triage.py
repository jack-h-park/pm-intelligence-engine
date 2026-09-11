"""Portfolio Triage (US-49) — cross-product relevance routing.

The funnel filter that runs once per signal *before* fan-out: a single LLM call
scores the signal against every product profile, and the engine applies the
relevance threshold to decide which products warrant a full run. Generalizes the
single-product auto-triage to the whole portfolio, ahead of the expensive stages.

Not a standard stage function: it runs before any run exists, so it takes no
RunContext/store and writes no stage output. The threshold is applied in code (not
by the LLM) so the cutoff stays authoritative here, mirroring auto-triage.
"""
# ruff: noqa: E501 — the long lines below are LLM prompt/schema text. Wrapping
# them would change what gets sent to the model, and a noqa on a specific line
# would become part of that prompt text.

from __future__ import annotations

from app.llm.json_call import complete_json
from app.llm.protocol import LLMProvider
from app.models.stages import PortfolioTriageOutput, ProductRelevance
from app.services.context_loader import ProductProfile

_JSON_SCHEMA = """{
  "products": [
    {"product_id": "<exact id from the profiles>", "relevance_score": <integer 1-5>, "reason": "<one sentence grounded in that product's profile>"}
  ]
}"""


def _profiles_block(profiles: list[ProductProfile]) -> str:
    return "\n\n".join(
        f"### {p.product_id}\nTitle: {p.title}\nOverview: {p.overview}" for p in profiles
    )


async def run(
    *,
    signal_id: str,
    title: str,
    summary: str,
    profiles: list[ProductProfile],
    llm: LLMProvider,
    threshold: int,
    pm_identity: str,
    framework: str | None = None,
) -> PortfolioTriageOutput:
    """Route one signal across the portfolio.

    ``framework`` is the triage prompt text; when None it is loaded from
    decision-context (prompts/portfolio/triage.md). ``profiles`` are the
    candidate products (origin product already excluded by the caller).
    Returns a verdict per profile, with ``relevant`` set by the threshold.
    """
    if not profiles:
        return PortfolioTriageOutput(signal_id=signal_id, threshold=threshold, products=[])

    if framework is None:
        from app.services.template_service import TemplateService
        from config import settings

        framework = TemplateService(settings.decision_system_root).load_portfolio_prompt("triage")

    system_message = (
        "You are a Product Manager. Follow the PM identity and operating philosophy "
        f"below.\n\n{pm_identity}"
    )
    user_message = f"""## Portfolio Triage Framework
{framework}

---

## Relevance Threshold
A product is relevant (warrants a full run) only when its relevance_score is >= {threshold}.

---

## Signal
Title: {title}
Summary:
{summary}

---

## Product Profiles
{_profiles_block(profiles)}

---

## Your Task
Score EVERY product profile above exactly once, using its exact product_id.
Respond with a single JSON object matching this schema exactly — no markdown, no commentary:

{_JSON_SCHEMA}"""

    data = await complete_json(
        llm,
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ],
        stage="portfolio_triage",
        run_id=signal_id,  # telemetry tag — no run exists yet at triage time
        max_tokens=1024,
        temperature=0,  # deterministic routing
    )

    # Index the LLM's verdicts by product_id; the engine, not the model, decides
    # `relevant` by applying the threshold.
    by_id = {item.get("product_id"): item for item in data.get("products", [])}
    products: list[ProductRelevance] = []
    for profile in profiles:
        item = by_id.get(profile.product_id)
        if item is None:
            # LLM omitted this product — treat as a non-relevant verdict rather
            # than silently dropping it from the audit trail.
            products.append(
                ProductRelevance(
                    product_id=profile.product_id,
                    relevance_score=1,
                    reason="No verdict returned by triage.",
                    relevant=False,
                )
            )
            continue
        score = int(item.get("relevance_score", 1))
        score = max(1, min(5, score))
        products.append(
            ProductRelevance(
                product_id=profile.product_id,
                relevance_score=score,
                reason=str(item.get("reason", "")),
                relevant=score >= threshold,
            )
        )

    return PortfolioTriageOutput(signal_id=signal_id, threshold=threshold, products=products)
