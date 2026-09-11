"""Canonical processing-mode vocabulary — the depth ladder (US-43).

Increasing depth: archive < note < structure < evaluate < decide.
Each mode says how far to process a signal:
  archive   — set aside, not pursued (noise / low relevance)        [S1 only]
  note      — record the insight                                    [+ S2 (+ S7)]
  structure — structure it into an opportunity                      [+ S3]
  evaluate  — full 4-persona evaluation, no routing                 [+ S4]
  decide    — full pipeline: routing decision + artifact            [S1–S7]

Canonical definition: pm-decision-context/core/02-workflow.md
("Processing Depth — 5 modes").

Renamed 2026-06-11: file→archive, brief→note, opportunity→structure. Legacy
values are normalized on input/read so stored data and existing clients (the
    operations plane)
keep working without changes.

NAMING NOTE — "archive" is overloaded across THREE unrelated concepts. They do
not refer to each other; do not infer the behavior of one from the name of
another:

  1. depth `archive` (THIS module)
       Gate 1's shallowest choice: signal acknowledged but NOT pursued (noise /
       low relevance). S1 only. Produces NO artifact and is the one depth that is
       deliberately EXCLUDED from the run-archive export (see run_finalizer
       `_EXPORTABLE_MODES`). "archive" here means *set aside*, NOT *store*.

  2. folder `archive/runs/<product>/<date>-<slug>/`
       The canonical run *repository of record* — browsable markdown trace of a
       completed run. Every depth note/structure/evaluate/decide is WRITTEN here.
       "archive" here means *store / keep*. This is why a `structure` run lands
       under `archive/` even though it is NOT depth `archive` — same word,
       opposite sense.
       This folder lives INSIDE THIS REPO (pm-intelligence-engine) —
       it is NOT WIKI_ROOT and NOT the wiki repo. pm-engine writes here directly;
       the wiki (a separate repo) is synced independently by the operations plane, never by
       pm-engine. `archive/runs/` ≠ WIKI_ROOT.

  3. "auto-triage archive" (ops plane / wiki_sync)
       Writing auto-killed signals to WIKI_ROOT/.../kills/auto-triaged/. Unrelated
       to both of the above. See docs/EXPORT_AND_SYNC_CONTRACT.md.

Decision (2026-06-20): keep the names as-is; disambiguate by documentation rather
than rename, to avoid a 3-repo migration (ops-plane watch paths, dashboard, on-disk
data). This block is that documentation.
"""

# Derived from the stage registry (app/pipeline.py) — the single source of truth
# for positions. Kept as a module constant so existing importers are unchanged;
# it is no longer an independent definition. The registry's stop rows, in order,
# are archive < note < structure < evaluate < decide.
from app import pipeline as _pipeline

MODES = _pipeline.depths()

_LEGACY_MODE_ALIASES = {
    "file": "archive",
    "brief": "note",
    "opportunity": "structure",
}


def normalize_mode(mode: str | None) -> str | None:
    """Map a legacy mode value to its canonical name; pass through canonical/None."""
    if mode is None:
        return None
    return _LEGACY_MODE_ALIASES.get(mode, mode)
