"""Unit tests for site-chrome stripping used by Stage 1."""

from app.stages.chrome import strip_chrome


def test_strips_docs_index_and_skip_to_content_banner():
    """The exact furniture that made run 116b5fee's S1 note 100% chrome: a
    documentation-index blockquote, a fetch-the-index line, and a Skip-to-content
    link ahead of the article."""
    raw = (
        "> ## Documentation Index\n"
        ">\n"
        "> Fetch the complete documentation index at: [/llms.txt](https://x.io/llms.txt)\n"
        ">\n"
        "> Use this file to discover all available pages before exploring further.\n"
        "\n"
        "[Skip to main content](https://x.io/docs#content-area)\n"
        "\n"
        "## Introduction\n"
        "This document provides security considerations for the Model Context Protocol, "
        "identifying attack vectors and best practices for implementers who need enough "
        "prose here to clear the minimum-kept threshold and prove the article survives.\n"
    )
    out = strip_chrome(raw)
    assert "Documentation Index" not in out
    assert "Skip to main content" not in out
    assert "Fetch the complete documentation index" not in out
    assert out.lstrip().startswith("## Introduction")
    assert "security considerations for the Model Context Protocol" in out


def test_unwraps_zero_width_anchor_links_but_keeps_heading_words():
    raw = (
        "## [​](https://x.io/docs#intro) Introduction\n"
        "Real article body long enough to be kept as the article rather than "
        "discarded by the minimum-length guard, describing the protocol in detail "
        "across several sentences so that the surviving prose comfortably clears "
        "the minimum-kept-characters threshold and the fallback path never fires.\n"
    )
    out = strip_chrome(raw)
    assert "Introduction" in out
    assert "https://x.io/docs#intro" not in out


def test_single_inline_link_in_wrapped_prose_is_kept():
    """A single link alone on a wrapped line is prose, not navigation — dropping
    it would delete a word from the middle of a sentence."""
    raw = (
        "This document should be read alongside the\n"
        "[MCP Authorization](https://x.io/spec)\n"
        "specification and the OAuth best-practices guide, with plenty of extra "
        "words so the whole passage clears the minimum-kept length comfortably.\n"
    )
    out = strip_chrome(raw)
    assert "MCP Authorization" in out
    assert "specification and the OAuth" in out


def test_drops_link_bars_with_several_links():
    raw = (
        "[Home](/a) [Docs](/b) [API](/c) [Blog](/d)\n"
        "The actual article content begins here and runs long enough to be treated "
        "as the real body rather than triggering the short-content fallback path, "
        "with enough additional prose that the kept text clears the minimum length "
        "and the navigation bar is the only thing removed from the captured page.\n"
    )
    out = strip_chrome(raw)
    assert "Home" not in out
    assert "The actual article content begins here" in out


def test_short_prose_is_returned_unchanged():
    """A genuinely short signal must pass through untouched rather than be
    blanked by aggressive stripping."""
    text = "Android 16 ships a new attestation API that changes how MDM verifies device integrity."
    assert strip_chrome(text) == text


def test_all_chrome_falls_back_to_original():
    """If stripping would leave less than the minimum, keep the original so the
    excerpt is never near-empty."""
    raw = "[Skip to main content](/c)\n[Sign in](/x) [Sign up](/y)\ncookie consent\n"
    assert strip_chrome(raw) == raw
