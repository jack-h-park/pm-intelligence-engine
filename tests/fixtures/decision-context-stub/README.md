# decision-context stub

The minimum the engine needs to construct in CI: one prompt per S4 persona, with
the `## Lens` and `## Evaluation Question` headings the template service parses.

The real decision-context repository is private, so CI runs against this stub
and deselects the tests that pin real content (`-m "not requires_decision_context"`).
`make test`, with `DECISION_CONTEXT_ROOT` pointing at the real checkout, runs
everything.
