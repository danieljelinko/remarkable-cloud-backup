"""Tests for rmbackup.py — no network calls, no rmapi binary required."""

import shutil
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
import rmbackup


# ---------------------------------------------------------------------------
# truncate_utf8
# ---------------------------------------------------------------------------

def test_truncate_utf8_ascii_no_cut():
    assert rmbackup.truncate_utf8("hello", 10) == "hello"

def test_truncate_utf8_ascii_exact():
    assert rmbackup.truncate_utf8("hello", 5) == "hello"

def test_truncate_utf8_ascii_over():
    assert rmbackup.truncate_utf8("hello world", 5) == "hello"

def test_truncate_utf8_unicode_smart_quote():
    # U+2019 RIGHT SINGLE QUOTATION MARK = 3 bytes (e2 80 99)
    s = "Rubik’s Cube"   # 13 chars, 15 bytes
    result = rmbackup.truncate_utf8(s, 8)
    # Must not cut in the middle of the 3-byte sequence
    result.encode("utf-8")    # would raise if invalid
    assert len(result.encode("utf-8")) <= 8

def test_truncate_utf8_cut_on_boundary():
    # 3 × U+2019 = 9 bytes; limit 7 → should keep only 2 (6 bytes)
    s = "’’’"
    result = rmbackup.truncate_utf8(s, 7)
    assert len(result.encode("utf-8")) <= 7
    result.encode("utf-8")   # valid UTF-8

def test_truncate_utf8_empty():
    assert rmbackup.truncate_utf8("", 10) == ""


# ---------------------------------------------------------------------------
# is_ecryptfs
# ---------------------------------------------------------------------------

MOUNTS_WITH_ECRYPTFS = textwrap.dedent("""\
    sysfs /sys sysfs rw 0 0
    /home/.ecryptfs/helinko/.Private /home/helinko ecryptfs rw,nosuid 0 0
    tmpfs /tmp tmpfs rw 0 0
""")

MOUNTS_NO_ECRYPTFS = textwrap.dedent("""\
    sysfs /sys sysfs rw 0 0
    /dev/sda1 / ext4 rw 0 0
    tmpfs /tmp tmpfs rw 0 0
""")

def test_is_ecryptfs_true(tmp_path):
    with patch("rmbackup.Path") as mock_path_cls:
        # Only intercept /proc/mounts reads
        real_path = Path
        def path_side_effect(arg=""):
            p = real_path(arg)
            if str(arg) == "/proc/mounts":
                m = MagicMock()
                m.read_text.return_value = MOUNTS_WITH_ECRYPTFS
                m.__str__ = lambda self: "/proc/mounts"
                return m
            return p
        mock_path_cls.side_effect = path_side_effect
        mock_path_cls.home.return_value = real_path.home()

        # Directly test the logic without the Path mock (simpler)
    with patch("builtins.open", side_effect=FileNotFoundError):
        pass  # skip open-based approach

    # Test the actual function with a patched read_text
    with patch.object(Path, "read_text", return_value=MOUNTS_WITH_ECRYPTFS):
        assert rmbackup.is_ecryptfs("/home/helinko/Work/something") is True

def test_is_ecryptfs_false():
    with patch.object(Path, "read_text", return_value=MOUNTS_NO_ECRYPTFS):
        assert rmbackup.is_ecryptfs("/home/helinko/Work/something") is False

def test_is_ecryptfs_tmp_not_encrypted():
    with patch.object(Path, "read_text", return_value=MOUNTS_WITH_ECRYPTFS):
        assert rmbackup.is_ecryptfs("/tmp/something") is False

def test_is_ecryptfs_proc_unreadable():
    with patch.object(Path, "read_text", side_effect=OSError("no permission")):
        assert rmbackup.is_ecryptfs("/home/helinko/x") is False


# ---------------------------------------------------------------------------
# parse_errors
# ---------------------------------------------------------------------------

CLEAN_OUTPUT = """\
downloading [/foo/bar.rmdoc]... OK
downloading [/foo/baz.rmdoc]... OK
"""

LONG_NAME_OUTPUT = """\
downloading [/foo/very long name.rmdoc]...ERROR: 2026/05/17 apictx.go:122: \
failed to copy /tmp/rmapizip123 to /backup/very long name.rmdoc, \
er: open /backup/very long name.rmdoc: file name too long
"""

NETWORK_OUTPUT = """\
downloading [/backup/BigBook.rmdoc]...ERROR: 2026/05/17 transport.go:258: \
http request failed with Get "https://internal.cloud.remarkable.com/sync/v3/files/abc": \
read tcp ...: connection reset by peer
"""

SCHEMA_OUTPUT = """\
ERROR: 2026/05/17 main.go:86: Error: schema version mismatch
"""

def test_parse_errors_clean():
    errs = rmbackup.parse_errors(CLEAN_OUTPUT)
    assert errs["long_name"] == []
    assert errs["network"] == []
    assert errs["schema"] is False

def test_parse_errors_long_name():
    errs = rmbackup.parse_errors(LONG_NAME_OUTPUT)
    assert len(errs["long_name"]) == 1
    assert "very long name.rmdoc" in errs["long_name"][0]
    assert errs["schema"] is False

def test_parse_errors_network():
    errs = rmbackup.parse_errors(NETWORK_OUTPUT)
    assert len(errs["network"]) == 1
    assert "BigBook.rmdoc" in errs["network"][0]

def test_parse_errors_schema():
    errs = rmbackup.parse_errors(SCHEMA_OUTPUT)
    assert errs["schema"] is True

def test_parse_errors_deduplication():
    doubled = LONG_NAME_OUTPUT + LONG_NAME_OUTPUT
    errs = rmbackup.parse_errors(doubled)
    assert len(errs["long_name"]) == 1   # deduped


# ---------------------------------------------------------------------------
# check_rmapi
# ---------------------------------------------------------------------------

def test_check_rmapi_present():
    with patch("rmbackup.shutil.which", return_value="/usr/local/bin/rmapi"):
        assert rmbackup.check_rmapi() is True

def test_check_rmapi_absent():
    with patch("rmbackup.shutil.which", return_value=None):
        assert rmbackup.check_rmapi() is False


# ---------------------------------------------------------------------------
# verify_backup
# ---------------------------------------------------------------------------

def test_verify_backup_ok(tmp_path):
    backup = tmp_path / "backup"
    backup.mkdir()
    for i in range(5):
        (backup / f"file{i}.rmdoc").write_bytes(b"x" * 1000)
    manifest = tmp_path / "manifest.txt"
    manifest.write_text("\n".join(f"[f] /file{i}" for i in range(5)))
    result = rmbackup.verify_backup(backup, manifest)
    assert result["status"] == "ok"
    assert result["file_count"] == 5
    assert result["manifest_count"] == 5

def test_verify_backup_empty(tmp_path):
    backup = tmp_path / "backup"
    backup.mkdir()
    result = rmbackup.verify_backup(backup, None)
    assert result["status"] == "empty"
    assert result["file_count"] == 0

def test_verify_backup_suspicious(tmp_path):
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "only.rmdoc").write_bytes(b"x")
    manifest = tmp_path / "manifest.txt"
    manifest.write_text("\n".join(f"[f] /file{i}" for i in range(100)))
    result = rmbackup.verify_backup(backup, manifest)
    assert result["status"] == "suspicious"

def test_verify_backup_no_manifest(tmp_path):
    backup = tmp_path / "backup"
    backup.mkdir()
    for i in range(3):
        (backup / f"f{i}.rmdoc").write_bytes(b"x")
    result = rmbackup.verify_backup(backup, None)
    assert result["manifest_count"] is None
    assert result["file_count"] == 3


# ---------------------------------------------------------------------------
# full_backup orchestration (mocked)
# ---------------------------------------------------------------------------

def test_full_backup_integration(tmp_path):
    """Verify orchestration order and error-fix flow without network calls."""
    backup_subdir = tmp_path / "remarkable-backup-20260101-000000"
    backup_subdir.mkdir()
    (backup_subdir / "file1.rmdoc").write_bytes(b"x")
    (backup_subdir / "file2.rmdoc").write_bytes(b"x")

    manifest_file = tmp_path / "manifest-20260101-000000.txt"
    manifest_file.write_text("[f] /file1\n[f] /file2\n")

    call_log = []

    with (
        patch("rmbackup.get_manifest", side_effect=lambda p: (p.write_text("[f] /file1\n[f] /file2\n"), 2)[1]) as m_manifest,
        patch("rmbackup.run_mget", return_value="downloading [/file1.rmdoc]... OK\n") as m_mget,
        patch("rmbackup.parse_errors", return_value={"long_name": [], "network": [], "schema": False}) as m_parse,
        patch("rmbackup.fix_long_filenames", return_value=(0, [])) as m_fix,
        patch("rmbackup.retry_network_errors", return_value=(0, [])) as m_retry,
        patch("rmbackup.verify_backup", return_value={"status": "ok", "file_count": 2, "manifest_count": 2, "size_bytes": 2}) as m_verify,
        patch("rmbackup.is_ecryptfs", return_value=False),
        patch("time.strftime", return_value="20260101-000000"),
    ):
        report = rmbackup.full_backup(tmp_path)

    # Orchestration order
    m_manifest.assert_called_once()
    m_mget.assert_called_once()
    m_parse.assert_called_once()
    m_fix.assert_not_called()    # no long_name errors
    m_retry.assert_not_called()  # no network errors
    m_verify.assert_called_once()

    assert report["status"] == "ok"
