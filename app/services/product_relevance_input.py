"""Product context items the relevance judgment may point at.

The engine, not the model, owns these items: a verdict names an item by a handle valid for one
call, and the engine copies the item's kind, section and text into the stored verdict.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from app.models.insights import ItemKind
from app.services.context_loader import ContextLoader, _extract_overview, _read_manifest

ITEM_LIMIT = 600
SECTION_LIMIT = 2000

_HEADINGS: tuple[tuple[ItemKind, tuple[str, ...]], ...] = (
    ("overview", ("product overview",)),
    ("pillar", ("strategy pillars", "strategic principles")),
    ("target_user", ("target users", "target customers")),
    ("constraint", ("product constraints",)),
)


@dataclass(frozen=True)
class ContextItem:
    item_ref: str
    kind: ItemKind
    section: str
    text: str


@dataclass(frozen=True)
class ProductInput:
    product_id: str
    title: str
    items: tuple[ContextItem, ...]
    revision: str


def _sections(context_md: str) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = []
    for line in context_md.splitlines():
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            sections.append((heading.group(1).strip(), []))
        elif sections:
            sections[-1][1].append(line)
    return sections


def _chosen_sections(context_md: str) -> list[tuple[ItemKind, str, list[str]]]:
    sections = _sections(context_md)
    chosen: list[tuple[ItemKind, str, list[str]]] = []
    for kind, names in _HEADINGS:
        for name in names:
            match = next((s for s in sections if s[0].lower() == name), None)
            if match is not None:
                chosen.append((kind, match[0], match[1]))
                break
    non_goal = next((s for s in sections if "non-goal" in s[0].lower()), None)
    if non_goal is not None:
        chosen.append(("non_goal", non_goal[0], non_goal[1]))
    return chosen


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text.replace("**", "")).strip()
    if len(text) > ITEM_LIMIT:
        text = text[: ITEM_LIMIT - 1] + "…"
    return text


def _blocks(lines: list[str]) -> list[str]:
    blocks: list[list[str]] = []
    current: list[str] | None = None
    in_subheading = False
    heading: str | None = None
    for line in lines:
        stripped = line.strip()
        subheading = re.match(r"^###\s+(.+?)\s*$", stripped)
        if subheading:
            heading = subheading.group(1)
            current = [heading + ":"]
            blocks.append(current)
            in_subheading = True
            continue
        if not stripped:
            if not in_subheading:
                current = None
            continue
        if stripped == "---" or stripped[0] in ">|#":
            continue
        if re.match(r"^([-*]|\d+\.)\s+", line):
            text = re.sub(r"^[-*]\s+", "", stripped)
            if heading is not None:
                text = f"{heading}: {text}"
            current = [text]
            blocks.append(current)
            in_subheading = False
            continue
        if current is None:
            current = [stripped]
            blocks.append(current)
        else:
            current.append(stripped)
    return [" ".join(block) for block in blocks]


def extract_items(product_id: str, context_md: str) -> list[ContextItem]:
    items: list[ContextItem] = []
    for kind, section, lines in _chosen_sections(context_md):
        total = 0
        count = 0
        for block in _blocks(lines):
            text = _clean(block)
            if not text or text.endswith(":"):
                continue
            if count and total + len(text) > SECTION_LIMIT:
                break
            count += 1
            total += len(text)
            items.append(ContextItem(f"{product_id}/{kind}/{count}", kind, section, text))
    return items


def _revision(items: list[ContextItem]) -> str:
    serialized = json.dumps(
        [[i.item_ref, i.kind, i.section, i.text] for i in items],
        separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProductInputs:
    """Loaded products, plus every allowlisted product that could not be loaded.

    `missing` holds (product_id, reason) with reason `missing`, `unreadable` or `no_items`. A
    caller must not judge relevance while `missing` is non-empty: comparing a partial set turns
    a tie into a confident answer.
    """

    products: list[ProductInput]
    missing: list[tuple[str, str]]


def build_product_inputs(loader: ContextLoader, eligible: list[str]) -> ProductInputs:
    """Inputs for allowlisted products, in allowlist order, and what could not be loaded."""
    inputs: list[ProductInput] = []
    missing: list[tuple[str, str]] = []
    for product_id in eligible:
        resolved = loader.resolve_product(product_id)
        if resolved is None:
            missing.append((product_id, "missing"))
            continue
        canonical, directory = resolved
        try:
            content = loader.load_product_context(canonical)
        except (OSError, UnicodeDecodeError):
            missing.append((product_id, "unreadable"))
            continue
        items = extract_items(canonical, content)
        if not items:
            missing.append((product_id, "no_items"))
            continue
        title, _overview = _extract_overview(content)
        # The manifest's display name, not the context H1: real contexts are headed
        # "Product Context — <name>", and the title is stored in immutable revisions.
        display = _read_manifest(directory).get("display_name")
        inputs.append(
            ProductInput(canonical, display or title or canonical, tuple(items), _revision(items))
        )
    return ProductInputs(inputs, missing)
