"""Runtime overrides for engine policy, layered over the configured defaults.

The engine's tunables arrive as pydantic settings, i.e. from the environment, so
changing one means editing `.env` and restarting the service. That is the right
mechanism for a deployment concern and the wrong one for a judgement the PM
revisits — the auto-triage threshold decides which signals are archived without
ever being shown to a human, and finding the right value means moving it and
watching what changes.

So the settings stay the baseline and a JSON file supplies deltas, read at call
time. An external configuration surface writes that file; nothing here writes it.

The shape matches every other override store in the fleet — keys nested under a
section name, with a sibling timestamp — so one convention covers the stores the
control-plane shell reads and this one:

    {"policy": {"AUTO_TRIAGE_THRESHOLD": 4}, "updated_at": "2026-07-19T..."}

Unset, absent or malformed, every resolver returns the configured value, so the
engine behaves exactly as it did before this existed. Reading per call rather
than caching is deliberate: a threshold that needed a restart to take effect
would have the same problem `.env` already has.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

# The store nests its keys under a section name, as every other override store in
# the fleet does.
_SECTION = "policy"

# Only these may be overridden. A general "apply whatever the file says to
# settings" would let a stray key silently retune something nobody was editing,
# including credentials and paths.
_OVERRIDABLE = {
    "AUTO_TRIAGE_THRESHOLD": int,
    "TRIAGE_RELEVANCE_THRESHOLD": int,
}


def _load() -> dict[str, Any]:
    from config import settings

    path = getattr(settings, "POLICY_OVERRIDES_FILE", "")
    if not path:
        return {}
    try:
        data = json.loads(pathlib.Path(path).read_text())
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001 — a bad override must never stop the pipeline
        return {}
    if not isinstance(data, dict):
        return {}
    section = data.get(_SECTION)
    return section if isinstance(section, dict) else {}


def resolve_int(name: str) -> int:
    """The effective value of an integer policy setting.

    Falls back to the configured setting when the file is absent, the key is
    missing, or the value is not a positive int — a zero or negative threshold
    would silently disable or invert the rule it governs.
    """
    from config import settings

    baseline = int(getattr(settings, name))
    if name not in _OVERRIDABLE:
        return baseline
    val = _load().get(name)
    if isinstance(val, bool):  # bool is an int subclass; not a threshold
        return baseline
    if isinstance(val, int) and val > 0:
        return val
    return baseline


def auto_triage_threshold() -> int:
    """S2 relevance strictly below this is archived without reaching Gate 1.

    Note for anyone reading a changed value and not seeing an effect: this is
    consulted only on the fully autonomous path. A run started with an explicit
    depth, or with force_gate1 (which the operator's own tooling sets for
    PM-initiated starts), skips auto-triage entirely.
    """
    return resolve_int("AUTO_TRIAGE_THRESHOLD")


def triage_relevance_threshold() -> int:
    """Portfolio Triage fan-out cutoff — a product needs at least this to run."""
    return resolve_int("TRIAGE_RELEVANCE_THRESHOLD")
