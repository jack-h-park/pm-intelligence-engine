# Insight question provenance and evidence fidelity

Status: Implementation verified locally; deployment and model-output evaluation pending.

## Findings

Production Candidate records retained their question IDs, but analysis constructed
every Insight with an empty question list. PreparedContext lacked an explicit
selected-question field. The worker currently selects only the first Candidate
question; tagging every Candidate question would overstate analysis coverage.

The Instinct bundle contained two supplied sources and one cited source. Its
reference to omitted material from both sources was therefore not disproved by
the one-source cited evidence view. Muse's comparison incorrectly labelled a
multi-day ChatGPT average as a launch-day figure. Multiple publications alone do
not establish independent reporting.

## Implementation and acceptance

Add a default-empty selected question list to PreparedContext. The worker copies
only its first selected Candidate question; the context loader records only its
matched configured interest. Analysis copies these Engine-owned IDs rather than
model-generated labels. Historical payloads still load without rewriting records.

Expose source IDs alongside passages in analysis input. Strengthen generation
instructions to preserve measurement windows, units, geography, attribution,
causal uncertainty, and the distinction between supplied and cited sources.
This prompt change is guidance, not a deterministic semantic validator or proof
that future model outputs are faithful.

Verify stored worker output retains the selected question, model labels cannot
override it, old context payloads load, and supplied passages expose their source
identity. Run Insight/Decision tests, lint, and strict typing.

## Remaining work

The worker still passes a question ID as question text rather than resolving the
configured natural-language interest. Wire configuration and context selection in
a follow-up with explicit missing-interest behavior. Evaluate model output on
measurement-window and causal-attribution cases before claiming fidelity solved.
Historical Muse content needs an approved immutable successor; this change does
not edit reviews or reprocess historical Insights. Instinct's pending human review
should use a funding-signal interpretation without inferring adoption or causation.
