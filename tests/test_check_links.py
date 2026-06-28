import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "crucible_check_links", Path(__file__).resolve().parent / "check_links.py")
check_links = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_links)

ROOT = check_links.ROOT


def test_link_is_valid_accepts_in_repo_file():
    assert check_links.link_is_valid(ROOT, "README.md", ROOT) is True


def test_link_is_valid_rejects_repo_escape():
    # ".." resolves to the repo's parent — it EXISTS but is outside the repo, so it must
    # be rejected (the old check only tested .exists(), so this would have passed).
    assert check_links.link_is_valid(ROOT, "..", ROOT) is False


def test_link_is_valid_rejects_missing_target():
    assert check_links.link_is_valid(ROOT, "does-not-exist.md", ROOT) is False


def test_find_broken_links_passes_on_repo():
    checked, broken = check_links.find_broken_links()
    assert broken == []
    assert checked >= 1
