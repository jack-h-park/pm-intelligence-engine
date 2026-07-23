"""Site-chrome removal for captured web pages.

`signals.raw_content` is whatever the capture pipeline scraped, and readability
extraction leaves a band of navigation furniture at the top of most docs sites:
a documentation-index banner, a "Skip to main content" link, and headings whose
text is preceded by a zero-width anchor link. S1 shows the *first N characters*
of that content, so on those pages the excerpt is 100% furniture and 0% article
— run 116b5fee showed a docs-index banner and three anchor links as the entire
"Stage 1" note while the article's own opening paragraph sat just past the cut.

Stripping is deliberately conservative: it drops lines that are recognisably
chrome and unwraps empty-text anchor links, but never reflows or summarises. The
result is still a verbatim excerpt of the source, just one that starts at the
prose.

Sibling implementation: the Gate 0 helper `sensing-product-propose.py` in
hermes-control-plane strips the same furniture before keyword scoring. The two
share patterns by convention, not by import — they run in different processes on
different machines.
"""

import re

# Chrome that survives readability extraction. Matched per line, case-insensitive.
_CHROME_PATTERNS = (
    r"skip to (main )?content",
    r"fetch the complete documentation index",
    r"use this file to discover all available pages",
    r"^\s*>?\s*#*\s*documentation index\s*$",
    r"you signed (in|out) with another tab",
    r"you switched accounts on another tab",
    r"reload to refresh your session",
    r"you must be signed in to",
    r"^\s*\[?(sign in|sign up|log in|login|register)\b",
    r"cookie banner|consent|privacy policy|terms of service|all rights reserved",
    r"^\s*(subscribe|newsletter|follow us|share this)\b",
    r"dismiss alert",
    r"was this page helpful",
    r"^\s*\{\{.*\}\}\s*$",
)
_CHROME_RE = re.compile("|".join(_CHROME_PATTERNS), re.I)

# A docs-site heading anchor: a link whose visible text is empty or a zero-width
# space. Removing the link keeps the heading's words, which are real content.
_ANCHOR_RE = re.compile(r"\[[\s​ ]*\]\([^)]*\)")

# A line that is mostly markdown links is navigation, not prose — but only when
# it carries *several* links. Prose wraps, so a single link can land alone on its
# own line ("complementing the / [MCP Authorization](…) / specification"), and
# dropping that line would silently delete a word out of the middle of a
# sentence. Real navigation bars run two or more links to a line.
_LINK_RE = re.compile(r"\[[^\]]*\]\([^)]*\)")
_LINK_DENSITY = 0.5
_MIN_NAV_LINKS = 2

# What is left of a line once its content is gone: blockquote markers, heading
# hashes, bullets, rules. Never worth a line of the excerpt on its own.
_PUNCT_ONLY_RE = re.compile(r"^[>#*\-_=|\s]*$")

# Below this, stripping removed so much that what is left is unlikely to be the
# article — fall back to the original rather than show a near-empty excerpt.
_MIN_KEPT_CHARS = 200


def strip_chrome(body: str) -> str:
    """Drop navigation/consent furniture from a captured page body.

    Returns `body` unchanged when stripping would leave too little to be
    plausibly the article — a page that is genuinely short, or one whose markup
    the patterns misread, is better shown raw than shown empty.
    """
    kept: list[str] = []
    for line in body.splitlines():
        s = _ANCHOR_RE.sub("", line).rstrip()
        stripped = s.strip()
        if not stripped:
            continue
        if _CHROME_RE.search(stripped):
            continue
        if _PUNCT_ONLY_RE.match(stripped):
            continue
        # Navigation: several links crowding one line. Character density catches
        # a link bar; the link count spares a wrapped prose line that happens to
        # contain a single inline link.
        links = _LINK_RE.findall(stripped)
        linked = sum(len(m) for m in links)
        if len(links) >= _MIN_NAV_LINKS and linked / len(stripped) > _LINK_DENSITY:
            continue
        kept.append(s)

    cleaned = "\n".join(kept)
    return cleaned if len(cleaned) >= _MIN_KEPT_CHARS else body
