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
values are normalized on input/read so stored data and existing clients (Hermes)
keep working without changes.
"""

MODES = ("archive", "note", "structure", "evaluate", "decide")

_LEGACY_MODE_ALIASES = {
    "file": "archive",
    "brief": "note",
    "opportunity": "structure",
}


def normalize_mode(mode):
    """Map a legacy mode value to its canonical name; pass through canonical/None."""
    if mode is None:
        return None
    return _LEGACY_MODE_ALIASES.get(mode, mode)
