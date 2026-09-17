# Insight question provenance and evidence fidelity

Status: Question-ID preservation deployed in #127. Configured-question resolution
is verified locally; deployment and model-output evaluation remain pending.

## Findings

Production Candidate records retained their question IDs, but analysis constructed
every Insight with an empty question list. PreparedContext lacked an explicit
selected-question field. The #127 worker selected only the first Candidate
question; tagging every Candidate question would overstate analysis coverage.

The Instinct bundle contained two supplied sources and one cited source. Its
reference to omitted material from both sources was therefore not disproved by
the one-source cited evidence view. Muse's comparison incorrectly labelled a
multi-day ChatGPT average as a launch-day figure. Multiple publications alone do
not establish independent reporting.

## Implementation and acceptance

Add a default-empty selected question list to PreparedContext. The worker now uses
the context loader to select the first configured interest in Candidate order,
record its actual question text and constraints, and retain only its matched ID.
Analysis receives the constraints and copies these Engine-owned IDs rather than
model-generated labels. Historical payloads still load without rewriting records.

Expose source IDs alongside passages in analysis input. Strengthen generation
instructions to preserve measurement windows, units, geography, attribution,
causal uncertainty, and the distinction between supplied and cited sources.
This prompt change is guidance, not a deterministic semantic validator or proof
that future model outputs are faithful.

Verify stored worker output retains the selected question, model labels cannot
override it, old context payloads load, and supplied passages expose their source
identity. Run Insight/Decision tests, lint, and strict typing.

Local validation: 684 tests passed with the companion decision-context configured
(`pytest -q -m 'not slow'`); Ruff and strict mypy passed. A run without companion
configuration failed the existing persona startup preflight, not Insight analysis.

## Remaining work

Configuration is required at analysis time. If a Candidate names interests but
none are registered, analysis fails before any model call and releases the lease
into the existing retryable-failure state. Missing configuration follows the same
path. Candidates with no question IDs use their subject without claiming an
interest match. Quiet evidence/no-new-learning terminal paths remain unchanged.
Fixture replay explicitly uses a checked-in fixture context, never operator assets.

The production configuration inspection found only `learning-loop` and
`decision-quality`. Historical sample IDs `enterprise-mobile-security` and
`personal-ai-agent-market` are not registered. Agree on the intended question text
and constraints in the decision-context repository before running those Candidates
again. Do not silently substitute a generic interest or mutate historical records.
This implementation does not activate acquisition, scheduling, delivery, or product
mapping. Rollback is the prior Engine revision; no schema migration is involved.

Evaluate model output on
measurement-window and causal-attribution cases before claiming fidelity solved.
Historical Muse content needs an approved immutable successor; this change does
not edit reviews or reprocess historical Insights. Instinct's pending human review
should use a funding-signal interpretation without inferring adoption or causation.
