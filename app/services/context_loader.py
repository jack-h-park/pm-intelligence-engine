import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class FullContext:
    pm_identity: str
    company_context: str
    product_context: str
    product_id: str


@dataclass
class ProductProfile:
    """Compressed product summary for Portfolio Triage (US-49).

    A short, relevance-judging profile derived from products/<id>/context.md —
    NOT the full context. Keeps the single Triage call cheap.
    """

    product_id: str
    title: str
    overview: str
    connection_anchors: list[str] = field(default_factory=list)


# Pseudo-products that are not real fan-out targets: the scaffold template and
# the 'general' catch-all (special-cased to archive/note only).
_NON_PRODUCT_DIRS = frozenset({"_template", "general"})


def _read_manifest(product_dir: Path) -> dict[str, Any]:
    """products/<dir>/product.yaml, or {} when absent or unreadable.

    The decision-context companion repo declares product identity there — id,
    display_name, aliases — so that a product can be renamed without breaking
    every reference already written down; its product-identity doc carries the
    contract. Resolving by that id rather than by the directory name is what
    keeps a signal captured before a rename pointing at the same product
    afterwards.

    Never raises: a malformed manifest must cost the alias, not the run.
    """
    try:
        data = yaml.safe_load((product_dir / "product.yaml").read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict) or not data.get("id"):
        return {}
    aliases = data.get("aliases") or []
    return {
        "id": str(data["id"]),
        "display_name": str(data.get("display_name") or data["id"]),
        "aliases": [str(a) for a in aliases if a],
    }


def _extract_overview(context_md: str) -> tuple[str, str]:
    """Return (title, overview) from a product context.md.

    title  = the leading '# ' heading.
    overview = the '## Product Overview' section body, falling back to the first
               '## ' section if Product Overview is absent.
    """
    title = ""
    sections: list[tuple[str, list[str]]] = []
    current: str | None = None
    buf: list[str] = []
    for line in context_md.splitlines():
        h1 = re.match(r"^#\s+(.+?)\s*$", line)
        h2 = re.match(r"^##\s+(.+?)\s*$", line)
        if h1 and not title:
            title = h1.group(1).strip()
        elif h2:
            if current is not None:
                sections.append((current, buf))
            current = h2.group(1).strip().lower()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections.append((current, buf))

    overview = ""
    for name, body in sections:
        if name == "product overview":
            overview = "\n".join(body).strip()
            break
    if not overview and sections:
        overview = "\n".join(sections[0][1]).strip()
    return title, overview


def _extract_connection_anchors(context_md: str) -> list[str]:
    """Return reviewed, literal connection anchors from a product context.

    These anchors are deliberately narrower than the product overview. They are
    used only by the read-only Insight connection assessor, never by the legacy
    Portfolio Triage path that can start a run.
    """
    anchors: list[str] = []
    in_section = False
    for line in context_md.splitlines():
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            in_section = heading.group(1).strip().lower() == "connection anchors"
            continue
        if not in_section:
            continue
        bullet = re.match(r"^\s*[-*]\s+(.+?)\s*$", line)
        if bullet:
            anchor = bullet.group(1).strip()
            if anchor:
                anchors.append(anchor)
    return anchors


class ContextLoader:
    def __init__(self, decision_system_root: str) -> None:
        self._root = Path(decision_system_root)

    def load_pm_identity(self) -> str:
        return (self._root / "core" / "00-pm-identity.md").read_text(encoding="utf-8")

    def load_company_context(self) -> str:
        return (self._root / "company-context.md").read_text(encoding="utf-8")

    def resolve_product(self, product_id: str) -> tuple[str, Path] | None:
        """(canonical id, directory) for an id, or None if nothing claims it.

        Matches the directory name first, which is the common case and costs one
        stat. Only when that misses does it read the manifests looking for a
        product that used to carry this id — an unrecognised id is more often a
        typo than a rename, and the cheap path should stay cheap.
        """
        products = self._root / "products"
        direct = products / product_id
        if direct.is_dir():
            manifest = _read_manifest(direct)
            return (manifest.get("id") or product_id), direct
        if not products.is_dir():
            return None
        for product_dir in sorted(products.iterdir()):
            if not product_dir.is_dir():
                continue
            manifest = _read_manifest(product_dir)
            if manifest and product_id in manifest["aliases"]:
                return manifest["id"], product_dir
        return None

    def load_product_context(self, product_id: str) -> str:
        resolved = self.resolve_product(product_id)
        if resolved is None:
            raise FileNotFoundError(
                f"Product context not found: {self._root / 'products' / product_id}"
            )
        path = resolved[1] / "context.md"
        if not path.exists():
            raise FileNotFoundError(f"Product context not found: {path}")
        return path.read_text(encoding="utf-8")

    def load_full_context(self, product_id: str) -> FullContext:
        resolved = self.resolve_product(product_id)
        # The canonical id, not the one we were handed. A run started from a
        # retired id should be stored and reported under the name the product
        # goes by now, or the rename leaves two ids in the database for one
        # product and every later group-by splits.
        canonical = resolved[0] if resolved else product_id
        return FullContext(
            pm_identity=self.load_pm_identity(),
            company_context=self.load_company_context(),
            product_context=self.load_product_context(product_id),
            product_id=canonical,
        )

    def load_portfolio_profiles(
        self, exclude: tuple[str, ...] = ()
    ) -> list[ProductProfile]:
        """Compressed profiles of every real product, for Portfolio Triage (US-49).

        Skips the scaffold (_template) and the 'general' catch-all, plus any
        product ids in ``exclude`` (e.g. the manually-named origin product, which
        is already getting a run). Products whose context.md is missing or has no
        extractable overview are skipped — Triage routes only over describable
        products.
        """
        products_root = self._root / "products"
        if not products_root.is_dir():
            return []

        skip = _NON_PRODUCT_DIRS.union(exclude)
        profiles: list[ProductProfile] = []
        for product_dir in sorted(products_root.iterdir()):
            if not product_dir.is_dir() or product_dir.name in skip:
                continue
            context_path = product_dir / "context.md"
            if not context_path.exists():
                continue
            content = context_path.read_text(encoding="utf-8")
            title, overview = _extract_overview(content)
            if not overview:
                continue
            manifest = _read_manifest(product_dir)
            product_id = manifest.get("id") or product_dir.name
            if product_id in skip:
                continue
            profiles.append(
                ProductProfile(
                    product_id=product_id,
                    title=title or manifest.get("display_name") or product_dir.name,
                    overview=overview,
                    connection_anchors=_extract_connection_anchors(content),
                )
            )
        return profiles
