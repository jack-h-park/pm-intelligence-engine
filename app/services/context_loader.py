from dataclasses import dataclass
from pathlib import Path


@dataclass
class FullContext:
    pm_identity: str
    company_context: str
    product_context: str
    product_id: str


class ContextLoader:
    def __init__(self, decision_system_root: str) -> None:
        self._root = Path(decision_system_root)

    def load_pm_identity(self) -> str:
        return (self._root / "core" / "00-pm-identity.md").read_text(encoding="utf-8")

    def load_company_context(self) -> str:
        return (self._root / "company-context.md").read_text(encoding="utf-8")

    def load_product_context(self, product_id: str) -> str:
        path = self._root / "products" / product_id / "context.md"
        if not path.exists():
            raise FileNotFoundError(f"Product context not found: {path}")
        return path.read_text(encoding="utf-8")

    def load_full_context(self, product_id: str) -> FullContext:
        return FullContext(
            pm_identity=self.load_pm_identity(),
            company_context=self.load_company_context(),
            product_context=self.load_product_context(product_id),
            product_id=product_id,
        )
