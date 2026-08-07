from pathlib import Path


def test_core_enforcement_does_not_import_optional_plugins() -> None:
    root = Path(__file__).parents[2] / "crew/security"
    production = "\n".join(
        path.read_text(encoding="utf-8")
        for path in root.glob("*.py")
        if path.name != "__init__.py"
    )
    assert "from plugins." not in production
    assert "import plugins." not in production
