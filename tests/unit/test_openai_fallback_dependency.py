import tomllib
from pathlib import Path


def test_openai_extra_installs_the_anthropic_fallback_sdk() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    requirements = pyproject["project"]["optional-dependencies"]["openai"]

    assert any(requirement.lower().startswith("anthropic") for requirement in requirements)
