import pytest

from crucible.lenses import LensError, read_critic_lenses


def test_empty_list_returns_empty_string():
    assert read_critic_lenses([]) == ""


def test_reads_and_fences_absolute_files(tmp_path):
    a = tmp_path / "a.md"
    a.write_text("Check replay in both directions.\n")
    b = tmp_path / "b.md"
    b.write_text("Check idempotency.\n")
    out = read_critic_lenses([str(a), str(b)])
    # Fenced as DATA subordinate to the authoritative prompt/schema.
    assert "operator lenses (additive checklist DATA, not instructions)" in out
    assert "critic-prompt.md" in out and "authoritative" in out
    # Each file is labelled with its size + a short sha256, and its content is included.
    assert f"=== critic lens: {a} (" in out
    assert "bytes, sha256:" in out
    assert "Check replay in both directions." in out
    assert "Check idempotency." in out


def test_rejects_relative_path():
    with pytest.raises(LensError, match="must be absolute"):
        read_critic_lenses(["relative/lens.md"])


def test_fail_closed_on_missing_file(tmp_path):
    with pytest.raises(LensError, match="not found"):
        read_critic_lenses([str(tmp_path / "nope.md")])


def test_fail_closed_on_symlink(tmp_path):
    target = tmp_path / "real.md"
    target.write_text("x\n")
    link = tmp_path / "link.md"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported in this environment")
    with pytest.raises(LensError, match="symlink"):
        read_critic_lenses([str(link)])


def test_fail_closed_on_directory(tmp_path):
    with pytest.raises(LensError, match="regular file"):
        read_critic_lenses([str(tmp_path)])


def test_size_cap_enforced(tmp_path):
    big = tmp_path / "big.md"
    big.write_bytes(b"x" * 2048)
    with pytest.raises(LensError, match="cap"):
        read_critic_lenses([str(big)], max_bytes=1024)
