# PM Intelligence Engine contributor notes

This repository contains a provider-neutral, human-gated signal-to-decision
workflow. Keep documentation and examples in English, preserve typed stage
contracts, and route all model calls through `LLMProvider`.

Development commands are documented in `README.md`. Do not commit credentials,
generated databases, run archives, private context, or deployment-specific
paths. Configure companion context and notification integrations through
environment variables supplied by the operator.

Some docs here, `docs/ARCHITECTURE.md` among them, feed sections of the author's
portfolio pages, and a drift report flags a section for review whenever a doc it
watches changes. Before committing a doc change, run
`gateway/scripts/portfolio-watched.py` from the hermes-control-plane checkout with
this checkout as the working directory. It lists the sections each staged file
feeds and the live page. Only if the change alters nothing those sections say, and
only after reading them on the live page, end each commit that touches the doc with
`Portfolio-Impact: none (<one-line reason>)`. Put it in the same last paragraph as
any `Co-Authored-By:` line; git ignores a trailer separated from it by a blank line.
Rules: hermes-control-plane `docs/design/doc-drift-chain-automation.md` § P12.
