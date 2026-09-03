# PM Intelligence Engine contributor notes

This repository contains a provider-neutral, human-gated signal-to-decision
workflow. Keep documentation and examples in English, preserve typed stage
contracts, and route all model calls through `LLMProvider`.

Development commands are documented in `README.md`. Do not commit credentials,
generated databases, run archives, private context, or deployment-specific
paths. Configure companion context and notification integrations through
environment variables supplied by the operator.
