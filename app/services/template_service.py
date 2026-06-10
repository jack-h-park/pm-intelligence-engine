import re
from pathlib import Path


def _parse_sections(text: str) -> dict:
    """Split a markdown document into {lowercased ## heading: body} sections.

    Only level-2 (``## ``) headings start a section; deeper headings and any
    other lines belong to the current section body. Bodies are stripped of
    surrounding whitespace so the parsed text matches the authored content.
    """
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = m.group(1).strip().lower()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


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

    def load_persona_prompt(self, persona: str) -> dict:
        """Load a single S4 persona's lens and evaluation question.

        Reads prompts/s4-personas/{persona}.md and parses its ``## Lens`` and
        ``## Evaluation Question`` sections. These section bodies are the
        engine-canonical persona prompt text (decision-context owns them);
        the engine no longer hardcodes persona wording.

        Returns {"lens": str, "question": str}.
        """
        path = self._prompts_root / "s4-personas" / f"{persona}.md"
        if not path.exists():
            raise FileNotFoundError(
                f"No persona prompt for '{persona}' at {path}"
            )
        sections = _parse_sections(path.read_text(encoding="utf-8"))
        try:
            return {"lens": sections["lens"], "question": sections["evaluation question"]}
        except KeyError as exc:
            raise ValueError(
                f"Persona prompt '{path.name}' is missing required section {exc} "
                "(expected '## Lens' and '## Evaluation Question')"
            ) from exc

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
