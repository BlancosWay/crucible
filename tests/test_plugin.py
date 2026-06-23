import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_command_file_exists_with_frontmatter():
    text = (ROOT / "commands" / "crucible.md").read_text()
    assert text.startswith("---")
    assert "crucible" in text.lower()


def test_plugin_json_valid():
    data = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert data["name"] == "crucible"
    assert "version" in data


def test_marketplace_json_valid_and_references_plugin():
    data = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert "plugins" in data
    names = [p.get("name") for p in data["plugins"]]
    assert "crucible" in names
