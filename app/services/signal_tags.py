"""Tag normalisation for signal labels.

The vocabulary is open (see ``SignalTag``) — this module does not decide *which*
tags are legitimate, only that they are written in one canonical shape so that
"Gate-1 Blocked", "gate_1_blocked" and "gate-1-blocked" are the same tag rather
than three. Normalising instead of rejecting keeps the labelling habit cheap to
form; the cost of a typo'd tag is a stray row, not a lost note.
"""

import re

# Bounds, not a vocabulary. They exist to keep a malformed or runaway caller from
# writing unbounded rows, and are generous enough that a real label never hits them.
MAX_TAG_LENGTH = 32
MAX_TAGS_PER_SIGNAL = 16

_NON_CANONICAL = re.compile(r"[^a-z0-9]+")


class TagError(ValueError):
    """A tag could not be normalised into a usable label."""


def normalize_tag(raw: str) -> str:
    """Canonicalise one tag to lowercase kebab-case.

    Raises ``TagError`` when nothing usable survives (empty / punctuation-only),
    since silently dropping such a tag would make an add look like it succeeded.
    """
    if not isinstance(raw, str):
        raise TagError(f"tag must be a string, got {type(raw).__name__}")
    tag = _NON_CANONICAL.sub("-", raw.strip().lower()).strip("-")
    if not tag:
        raise TagError(f"tag {raw!r} normalises to an empty label")
    if len(tag) > MAX_TAG_LENGTH:
        raise TagError(
            f"tag {tag!r} exceeds {MAX_TAG_LENGTH} characters "
            "— tags are labels, use a note for anything longer"
        )
    return tag


def normalize_tags(raw_tags: list[str]) -> list[str]:
    """Normalise a batch, dropping duplicates while preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in raw_tags:
        tag = normalize_tag(raw)
        if tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out
