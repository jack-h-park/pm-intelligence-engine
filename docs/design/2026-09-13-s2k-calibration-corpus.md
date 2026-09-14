# S2K Calibration Corpus

## Purpose

The S2K fixture corpus is the release gate for semantic behavior, not a count of successful worker calls. It fixes twelve representative cases and their expected outcomes before any scanner or Decision pipeline activation is reconsidered.

## Current boundary

The existing replay proves only the store and worker happy path. It can distinguish a missing body (`needs_evidence`) from a body that can be analyzed. It does not yet enforce duplicate-without-delta, stale, or superseded dispositions. Those cases are deliberately recorded as `no_new_learning` expectations so a future evaluator cannot silently classify them as successful new Insights.

## Corpus requirements

Every case contains a source/candidate fixture and asserts:

* expected S2K disposition: `ready`, `needs_evidence`, or `no_new_learning`;
* expected read-only product-connection assessment;
* candidate product IDs only when the evidence supports them;
* required cited passage IDs only when an Insight is expected.

The corpus has no network access and makes no OAuth, search, paid-fetch, Signal, run, DecisionCase, or delivery write.

## Next implementation gate

Wire the corpus to a deterministic evaluator. Tests must fail if a duplicate, stale, superseded, or provenance-deficient case emits a ready Insight. Only after that evaluator and the read-only product-connection contract pass together should varied manual Insight samples be reviewed.
