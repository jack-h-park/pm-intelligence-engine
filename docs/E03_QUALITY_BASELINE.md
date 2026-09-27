# E03 semantic quality baseline

**Status:** Partial baseline, 2026-09-27. This document records what is reproducible today and what remains unmeasured. It does not authorize the sensing cutover.

## Reproducible fixture boundary

`tests/fixtures/signal_intelligence/cases.json` remains the existing 12-case worker and routing corpus. The separate `quality_cases.json` freezes 12 synthetic semantic scenarios with source text, source version, SHA-256, passage IDs, expected disposition, expected facts, unknowns, and a usefulness criterion. It covers practical learning, commercial judgment, platform mechanism, strong and weak historical-output patterns, late evidence, thin evidence, old useful context, superseded releases, repeated events with and without a delta, and no agent handoff. These are designed acceptance inputs; they are not sampled production outcomes or evidence of model quality.

Run the offline evaluator without a provider call or notification:

```bash
python -m scripts.evaluate_insights \
  --cases tests/fixtures/signal_intelligence/quality_cases.json \
  --results tests/fixtures/signal_intelligence/quality_results.empty.json \
  --reviews tests/fixtures/signal_intelligence/quality_reviews.empty.json \
  --output /tmp/e03-quality-baseline.json
```

The 2026-09-27 output contains 12 cases: eight expected `ready`, three `no_new_learning`, and one `needs_evidence`. All source hashes and expected passage references validate. There are zero supplied replays and zero human reviews, so **0 accepted, 0 failed, and 12 need review**. The denominator for a useful-output rate is zero; no rate can be reported. Costs and latency are unknown. A missing replay is never treated as a bad insight or a successful one.

The evaluator keeps same-evidence and enriched runs separate. It records disposition, claims, source references, model, prompt revision, source cost, LLM cost, latency, and human reviewer identity. Four quality dimensions—explanation, freshness, relevance, and takeaway—each require an evidence-backed human verdict. A positive verdict must cite a frozen passage; an LLM self-score or an unknown cost cannot pass. A factual or citation failure remains visible as a failure rather than being averaged away. Enrichment sources require their own version and verified hash.

## Historical calibration that is available

The PalmClaw case in the control-plane `docs/design/signal-output-comparison.md` was checked again through the authenticated production API on 2026-09-27. Signal `6d56981f-7d3f-44b3-9cbd-b7bdda734723` retains a 35,894-byte `raw_content` field with SHA-256 `e485924f36abe599a4f4dced0876f8e789ac52ad9d0ee4301175510266ea3829`. Run `acf718ac-0e57-4983-a828-07c5af50ef11` retains S1 output version 1, 1,270 bytes, SHA-256 `e9cf965144f727fdf44ceb8a17811be5041f76b61b91b44e906357776f8c347a`; S2 output version 1, 1,912 bytes, SHA-256 `95fd8aac970035096c7807191f48f45204603a851be7ad62d5d52177f4928ee3`. Hashes are over the exact UTF-8 strings returned by the API, with no normalization. The run completed at S2 with depth `note`. This is one selected diagnostic case, not a random sample.

The retained raw body and stage outputs do **not** establish the exact prompt, context assembly, evidence budget, or model configuration sent to S2. No same-input historical replay is claimed. The authored comparison in the control-plane document remains a qualitative diagnostic, not a provider result. The 12 synthetic cases therefore retain `historical.status=not_preserved`.

## Remaining E03 acceptance work

1. Freeze a bounded set of actual selected and excluded candidates, including strong historical outputs and known important omissions. Store permitted source snapshots and old output versions with provenance. Keep the selection denominator and limits explicit.
2. Generate cached candidate results for the 12 semantic cases in an isolated fixture path with notifications disabled, keeping same-evidence and enriched reading distinct. Record effective model, prompt revision, source/LLM cost, latency, and the evidence budget. Do not infer a comparison for missing historical inputs.
3. Obtain independent human review against cited passages, then report each dimension, factual failures, omissions, and useful-output denominator. This is the gate for representative quality, not the mere existence of the evaluator.

The existing live pilot's one cited Insight and mechanism tests do not satisfy these steps. The $5 cap approved for one controlled run is not a default daily or monthly spending policy.
