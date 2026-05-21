import re
from pathlib import Path


class TemplateService:
    def __init__(self, decision_system_root: str) -> None:
        self._prompts_root = Path(decision_system_root) / "prompts"

    def load_template(self, stage: str, version: str = "latest") -> str:
        """Load the prompt template for a stage.

        Looks first in a versioned subdirectory (prompts/s2/), then falls back
        to a flat file matching prompts/s2-*.md.
        """
        stage_dir = self._prompts_root / stage
        if stage_dir.is_dir():
            files = sorted(stage_dir.glob("*.md"))
            if files:
                return files[-1].read_text(encoding="utf-8")

        # Flat file fallback: prompts/s2-insight-extraction.md
        matches = sorted(self._prompts_root.glob(f"{stage}-*.md"))
        if matches:
            return matches[0].read_text(encoding="utf-8")

        raise FileNotFoundError(
            f"No template found for stage '{stage}' in {self._prompts_root}"
        )

    def render_template(self, template: str, variables: dict) -> str:
        """Substitute {variable_name} placeholders in the template.

        Raises ValueError if the template contains a placeholder that is not
        present in variables.
        """
        # Match single-brace placeholders that are not double-braced escapes
        placeholders = set(re.findall(r"(?<!\{)\{([^{}]+)\}(?!\})", template))
        if placeholders:
            missing = placeholders - set(variables.keys())
            if missing:
                raise ValueError(
                    f"Template is missing required variables: {sorted(missing)}"
                )
            return template.format(**variables)
        return template
