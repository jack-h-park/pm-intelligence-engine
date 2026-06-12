"""Eval harness — Phase 2.

Runs S1–S5 against the golden scenarios in scenarios.json and verifies:
  - All outputs are valid Pydantic models
  - S3 hypothesis passes the falsifiability rubric
  - S4 rubric score >= 9/12
  - S5 routing matches expected value
  - S5 composite falls within expected range
  - All outputs are persisted to the store

Usage:
    python eval/runner.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.factory import build_engine
from app.models.stages import RunContext, S1Input, S2Input, S3Input, S4Input, S5Input
from app.stages import s1_signal, s2_insight, s3_opportunity, s4_evaluation, s5_prioritization
from config import settings
from eval.rubrics import s3_hypothesis as s3_rubric
from eval.rubrics import s5_routing as s5_rubric

SCENARIOS_PATH = Path(__file__).parent / "scenarios.json"


def resolve_eval_model_name(settings_obj: Any) -> str:
    provider = settings_obj.LLM_PROVIDER.lower()
    if provider == "claude":
        return settings_obj.ANTHROPIC_MODEL
    if provider == "openai":
        return settings_obj.OPENAI_MODEL
    raise ValueError(f"Unsupported LLM_PROVIDER '{settings_obj.LLM_PROVIDER}' for eval")


def resolve_eval_runtime(settings_obj: Any) -> tuple[str, str]:
    provider = settings_obj.LLM_PROVIDER.lower()
    model = resolve_eval_model_name(settings_obj)
    return provider, model


def load_scenarios() -> list[dict]:
    return json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))


async def run_scenario(scenario: dict[str, Any], engine) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    result: dict[str, Any] = {
        "run_id_label": scenario["run_id"],
        "product_id": scenario["product_id"],
        "passed": False,
        "issues": [],
        "s1": None,
        "s2": None,
        "s3": None,
        "s4": None,
        "s5": None,
        "hypothesis_check": None,
        "rubric_check": None,
        "routing_check": None,
        "elapsed_s": 0.0,
    }

    start = time.monotonic()
    try:
        signal_id = engine.store.save_signal(
            original_product_id=scenario["product_id"],
            title=scenario["title"],
            raw_content=scenario["signal_text"],
            source_url=scenario.get("source_url"),
            category="other",
            source_type="manual",
        )

        run_id = engine.store.create_run(
            product_id=scenario["product_id"],
            signal_id=signal_id,
        )
        engine.store.update_run(run_id, status="running", current_stage="s1")

        full_ctx = engine.context_loader.load_full_context(scenario["product_id"])
        context = RunContext(
            run_id=run_id,
            product_id=scenario["product_id"],
            pm_identity=full_ctx.pm_identity,
            company_context=full_ctx.company_context,
            product_context=full_ctx.product_context,
        )

        # --- S1 ---
        engine.store.update_run(run_id, current_stage="s1")
        s1_out = await s1_signal.run(
            input=S1Input(
                signal_id=signal_id,
                title=scenario["title"],
                raw_content=scenario["signal_text"],
                source_url=scenario.get("source_url"),
            ),
            context=context,
            llm=engine.llm,
            store=engine.store,
        )
        result["s1"] = {"valid": True, "title": s1_out.output.title}

        # --- S2 ---
        engine.store.update_run(run_id, current_stage="s2")
        s2_out = await s2_insight.run(
            input=S2Input(
                signal_id=signal_id,
                s1_output=s1_out.output,
                product_id=scenario["product_id"],
            ),
            context=context,
            llm=engine.llm,
            store=engine.store,
        )
        pillars = s2_out.output.pillar_references
        if not pillars:
            result["issues"].append("S2: pillar_references is empty")
        result["s2"] = {"valid": True, "pillar_count": len(pillars)}

        # --- S3 ---
        engine.store.update_run(run_id, current_stage="s3")
        s3_out = await s3_opportunity.run(
            input=S3Input(
                signal_id=signal_id,
                s2_output=s2_out.output,
                product_id=scenario["product_id"],
            ),
            context=context,
            llm=engine.llm,
            store=engine.store,
        )
        hyp = s3_out.output.hypothesis
        hyp_check = s3_rubric.check(hyp)
        result["s3"] = {"valid": True, "hypothesis": hyp[:120] + "…" if len(hyp) > 120 else hyp}
        result["hypothesis_check"] = {
            "passed": hyp_check.passed,
            "score": f"{hyp_check.score}/3",
            "issues": hyp_check.issues,
        }
        if not hyp_check.passed:
            result["issues"].extend(hyp_check.issues)

        # --- S4 (4 parallel agents) ---
        engine.store.update_run(run_id, current_stage="s4")
        s4_out = await s4_evaluation.run(
            S4Input(s3_output=s3_out.output),
            context,
            engine.llm,
            engine.store,
        )
        rubric = s4_out.output.rubric
        result["s4"] = {
            "valid": True,
            "rubric_score": rubric.total_score,
            "rubric_passed": rubric.passed,
            "scores": {p.persona: p.score for p in s4_out.output.personas},
        }
        result["rubric_check"] = {
            "passed": rubric.passed,
            "score": f"{rubric.total_score}/12",
            "issues": rubric.issues,
        }
        if not rubric.passed:
            result["issues"].extend([f"S4 rubric: {i}" for i in rubric.issues])

        # --- S5 (scoring + routing) ---
        engine.store.update_run(run_id, current_stage="s5")
        s5_out = await s5_prioritization.run(
            S5Input(s4_output=s4_out.output),
            context,
            engine.llm,
            engine.store,
        )
        routing_result = s5_rubric.check(
            run_id=scenario["run_id"],
            actual_routing=s5_out.output.routing,
            actual_composite=s5_out.output.composite_score,
            expected_routing=scenario["expected_routing"],
            expected_composite_range=tuple(scenario["expected_composite_range"]),
        )
        result["s5"] = {
            "valid": True,
            "routing": s5_out.output.routing,
            "composite": s5_out.output.composite_score,
            "blocking_count": s5_out.output.blocking_count,
        }
        result["routing_check"] = {
            "passed": routing_result.passed,
            "routing_passed": routing_result.routing_passed,
            "composite_passed": routing_result.composite_passed,
            "issues": routing_result.issues,
        }
        if not routing_result.passed:
            result["issues"].extend(routing_result.issues)

        final_status = "waiting_approval" if s5_out.output.routing != "kill" else "completed"
        engine.store.update_run(run_id, status=final_status, current_stage="s4")

        result["passed"] = len(result["issues"]) == 0

    except Exception as exc:  # noqa: BLE001
        result["issues"].append(f"Exception: {type(exc).__name__}: {exc}")

    result["elapsed_s"] = round(time.monotonic() - start, 2)
    return result


async def main() -> int:
    scenarios = load_scenarios()
    engine = build_engine("local")
    provider, actual_model = resolve_eval_runtime(settings)

    print(f"\n{'=' * 60}")
    print("  PM Agentic Platform — Eval Harness (Phase 2)")
    print(f"  Runtime:  {provider} / {actual_model}")
    print(f"  Scenarios: {len(scenarios)}")
    print(f"{'=' * 60}\n")

    all_passed = True
    for scenario in scenarios:
        print(f"▶  {scenario['run_id']}  {scenario['title'][:55]}")
        result = await run_scenario(scenario, engine)

        status = "✓ PASS" if result["passed"] else "✗ FAIL"
        print(f"   {status}  ({result['elapsed_s']}s)")

        if result["s1"]:
            print(f"   S1  valid={result['s1']['valid']}")
        if result["s2"]:
            print(f"   S2  pillars={result['s2']['pillar_count']}")
        if result["s3"]:
            hyp = result["s3"]["hypothesis"]
            print(f"   S3  hypothesis: {hyp}")
        if result["hypothesis_check"]:
            hc = result["hypothesis_check"]
            print(f"   S3 rubric  {hc['score']}  passed={hc['passed']}")
            for issue in hc["issues"]:
                print(f"     ⚠  {issue}")
        if result["s4"]:
            s4 = result["s4"]
            scores_str = "  ".join(f"{k}={v}" for k, v in s4["scores"].items())
            print(f"   S4  scores: {scores_str}")
        if result["rubric_check"]:
            rc = result["rubric_check"]
            print(f"   S4 rubric  {rc['score']}  passed={rc['passed']}")
            for issue in rc["issues"]:
                print(f"     ⚠  {issue}")
        if result["s5"]:
            s5 = result["s5"]
            print(
                f"   S5  composite={s5['composite']}  routing={s5['routing']}"
                f"  blocking={s5['blocking_count']}"
            )
        if result["routing_check"]:
            rtc = result["routing_check"]
            print(f"   S5 routing  passed={rtc['passed']}")
            for issue in rtc["issues"]:
                print(f"     ⚠  {issue}")

        for issue in result["issues"]:
            rubric_issues = (result.get("rubric_check") or {}).get("issues", [])
            already_shown = (
                issue in (result.get("hypothesis_check") or {}).get("issues", [])
                or issue in (result.get("routing_check") or {}).get("issues", [])
                or any(issue == f"S4 rubric: {rubric_issue}" for rubric_issue in rubric_issues)
            )
            if not already_shown:
                print(f"   ✗  {issue}")
        print()

        if not result["passed"]:
            all_passed = False

    print(f"{'=' * 60}")
    if all_passed:
        print("  Result: ALL SCENARIOS PASSED")
    else:
        print("  Result: SOME SCENARIOS FAILED")
    print(f"{'=' * 60}\n")

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
