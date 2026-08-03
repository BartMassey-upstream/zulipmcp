"""Tests for _consume_interrupt_file — the listen() interrupt-file consumer.

Contract: claim-then-read (atomic rename before reading, so a concurrent
writer can never have its content deleted unread) and UTF-8 decoding with
errors="replace" (invalid bytes must degrade to U+FFFD, not raise and leave
the file in place to fail every subsequent poll).
"""
from pathlib import Path

from zulipmcp.mcp import _consume_interrupt_file


def _interrupt(tmp_path: Path, data: bytes) -> Path:
    path = tmp_path / ".listen_interrupt"
    path.write_bytes(data)
    return path


def test_missing_file_returns_none(tmp_path: Path) -> None:
    assert _consume_interrupt_file(tmp_path / ".listen_interrupt") is None


def test_valid_utf8_consumed(tmp_path: Path) -> None:
    path = _interrupt(tmp_path, "wake up — subagent done\n".encode())
    assert _consume_interrupt_file(path) == "wake up — subagent done\n"
    assert not path.exists()


def test_empty_file_returns_empty_string_not_none(tmp_path: Path) -> None:
    path = _interrupt(tmp_path, b"")
    assert _consume_interrupt_file(path) == ""
    assert not path.exists()


def test_invalid_bytes_replaced_not_raised(tmp_path: Path) -> None:
    path = _interrupt(tmp_path, b"before \xff\xfe mid \xc3\x28 after MARKER")
    content = _consume_interrupt_file(path)
    assert content is not None
    assert "MARKER" in content
    assert "�" in content
    assert not path.exists()


def test_poisoned_file_does_not_wedge_subsequent_polls(tmp_path: Path) -> None:
    """Pre-fix failure mode: strict decode raised before the delete, so the
    poisoned file survived and every later poll re-read and re-failed."""
    path = _interrupt(tmp_path, b"\xff")
    assert _consume_interrupt_file(path) == "�"
    assert not path.exists()
    assert _consume_interrupt_file(path) is None  # channel clear, not wedged


def test_no_claim_residue_left_behind(tmp_path: Path) -> None:
    path = _interrupt(tmp_path, b"content")
    _consume_interrupt_file(path)
    assert list(tmp_path.iterdir()) == []


def test_stale_claim_file_is_replaced_not_fatal(tmp_path: Path) -> None:
    """A crash between claim and unlink strands a .claimed file; the next
    consume must still succeed (atomic replace overwrites it)."""
    stale = tmp_path / ".listen_interrupt.claimed"
    stale.write_bytes(b"stranded")
    path = _interrupt(tmp_path, b"fresh content")
    assert _consume_interrupt_file(path) == "fresh content"
    assert list(tmp_path.iterdir()) == []


def test_claim_removes_live_path_before_read(tmp_path: Path, monkeypatch) -> None:
    """The live path must be gone by the time the content is read — that is
    the property that makes a concurrent writer's replace-after-claim safe
    (the writer starts a fresh file instead of having its append deleted)."""
    path = _interrupt(tmp_path, b"claimed content")

    original_read_text = Path.read_text
    live_path_seen_during_read = []

    def spying_read_text(self: Path, *args, **kwargs) -> str:
        # Deliberately checks the outer `path` (the live location), not
        # `self` (the claim file being read) — the property under test is
        # that the live path is already gone when the read happens.
        live_path_seen_during_read.append(path.exists())
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spying_read_text)
    assert _consume_interrupt_file(path) == "claimed content"
    assert live_path_seen_during_read == [False]


def test_claim_failure_returns_none_and_leaves_live_file(
    tmp_path: Path, monkeypatch
) -> None:
    """A failed claim (e.g. PermissionError) must not raise and must leave
    the live file untouched so the next poll can retry."""
    path = _interrupt(tmp_path, b"content")

    def failing_replace(self: Path, target) -> None:
        raise PermissionError("claim denied")

    monkeypatch.setattr(Path, "replace", failing_replace)
    assert _consume_interrupt_file(path) is None
    monkeypatch.undo()
    assert path.read_bytes() == b"content"


def test_read_failure_returns_none_without_raising(
    tmp_path: Path, monkeypatch
) -> None:
    path = _interrupt(tmp_path, b"content")

    def failing_read_text(self: Path, *args, **kwargs) -> str:
        raise OSError("disk error")

    monkeypatch.setattr(Path, "read_text", failing_read_text)
    assert _consume_interrupt_file(path) is None


def test_unlink_failure_still_delivers_content(
    tmp_path: Path, monkeypatch
) -> None:
    """A cleanup failure after a successful read must not mask the content
    — delivery wins over tidiness (the stale claim file is overwritten by
    the next claim)."""
    path = _interrupt(tmp_path, b"content")

    def failing_unlink(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError("unlink denied")

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    assert _consume_interrupt_file(path) == "content"
