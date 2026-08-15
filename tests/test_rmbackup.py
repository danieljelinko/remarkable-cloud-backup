"""Tests for rmbackup.py — no network calls, no rmapi binary required."""

import json
import shutil
import textwrap
from datetime import datetime, timezone
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

# ---------------------------------------------------------------------------
# ls_json
# ---------------------------------------------------------------------------

LS_JSON_OUTPUT = json.dumps([
    {
        "ID": "abc1", "VissibleName": "My Notes", "Type": "DocumentType",
        "ModifiedClient": "2026-05-20T10:00:00Z", "ModifiedServer": "2026-05-20T10:01:00Z",
        "Parent": "", "Version": 3,
    },
    {
        "ID": "abc2", "VissibleName": "Subfolder", "Type": "CollectionType",
        "ModifiedClient": "2026-05-18T08:00:00Z", "ModifiedServer": "2026-05-18T08:01:00Z",
        "Parent": "", "Version": 1,
    },
])

def test_ls_json_parses_output():
    with patch("rmbackup.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=LS_JSON_OUTPUT)
        entries = rmbackup.ls_json("/")
    assert len(entries) == 2
    assert entries[0]["VissibleName"] == "My Notes"

def test_ls_json_returns_empty_on_error():
    with patch("rmbackup.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        assert rmbackup.ls_json("/") == []

def test_ls_json_returns_empty_on_bad_json():
    with patch("rmbackup.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="not json")
        assert rmbackup.ls_json("/") == []


# ---------------------------------------------------------------------------
# list_recent
# ---------------------------------------------------------------------------

# All files in root so only one ls_json call is made
FIND_OUTPUT = "[f] /My Notes\n[f] /Old Doc\n"

def test_list_recent_filters_by_date():
    recent_entry = {
        "ID": "x1", "VissibleName": "My Notes", "Type": "DocumentType",
        "ModifiedClient": "2026-05-21T10:00:00Z",  # 1 day before cutoff → inside window
    }
    old_entry = {
        "ID": "x2", "VissibleName": "Old Doc", "Type": "DocumentType",
        "ModifiedClient": "2026-05-01T10:00:00Z",  # 21 days before cutoff → outside window
    }

    with (
        patch("rmbackup.subprocess.run") as mock_run,
        patch("rmbackup.ls_json", return_value=[recent_entry, old_entry]),
        patch("rmbackup.datetime") as mock_dt,
    ):
        fixed_now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat.side_effect = datetime.fromisoformat
        mock_run.return_value = MagicMock(returncode=0, stdout=FIND_OUTPUT)

        results = rmbackup.list_recent(days=7)

    assert len(results) == 1
    assert results[0]["VissibleName"] == "My Notes"

def test_list_recent_detects_modified_when_rmapi_uses_camelcase_schema():
    # Given rmapi >=0.0.34 emits camelCase keys (type/modifiedClient/name), not the old capitalized schema
    recent_entry = {
        "id": "x1", "name": "My Notes", "type": "DocumentType",
        "modifiedClient": "2026-05-21T10:00:00Z",  # 1 day before cutoff → inside window
    }
    old_entry = {
        "id": "x2", "name": "Old Doc", "type": "DocumentType",
        "modifiedClient": "2026-05-01T10:00:00Z",  # 21 days before cutoff → outside window
    }

    # When we list files modified in the last 7 days
    with (
        patch("rmbackup.subprocess.run") as mock_run,
        patch("rmbackup.ls_json", return_value=[recent_entry, old_entry]),
        patch("rmbackup.datetime") as mock_dt,
    ):
        fixed_now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat.side_effect = datetime.fromisoformat
        mock_run.return_value = MagicMock(returncode=0, stdout=FIND_OUTPUT)

        results = rmbackup.list_recent(days=7)

    # Then the camelCase entry inside the window is detected and its path built from `name`
    assert len(results) == 1
    assert results[0]["_cloud_path"] == "/My Notes"

def test_list_recent_empty_when_no_files():
    with patch("rmbackup.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        results = rmbackup.list_recent(days=7)
    assert results == []


# ---------------------------------------------------------------------------
# fetch_file
# ---------------------------------------------------------------------------

def test_fetch_file_success(tmp_path):
    cloud_path = "/Work/Meeting notes"
    fake_download = tmp_path / "tmp_dl"
    fake_download.mkdir()
    fake_file = fake_download / "Meeting notes.rmdoc"
    fake_file.write_bytes(b"rmdoc content")

    with (
        patch("rmbackup._rmapi_get", return_value=fake_file),
        patch("rmbackup.is_ecryptfs", return_value=False),
    ):
        result = rmbackup.fetch_file(cloud_path, tmp_path / "out")

    assert result is not None
    assert result.suffix == ".rmdoc"
    assert result.exists()

def test_fetch_file_truncates_on_ecryptfs(tmp_path):
    long_name = "A" * 200
    cloud_path = f"/folder/{long_name}"
    fake_download = tmp_path / "tmp_dl"
    fake_download.mkdir()
    fake_file = fake_download / f"{long_name}.rmdoc"
    fake_file.write_bytes(b"x")

    with (
        patch("rmbackup._rmapi_get", return_value=fake_file),
        patch("rmbackup.is_ecryptfs", return_value=True),
    ):
        result = rmbackup.fetch_file(cloud_path, tmp_path / "out")

    assert result is not None
    assert len(result.name.encode("utf-8")) <= rmbackup.ECRYPTFS_SAFE_BYTES

def test_fetch_file_returns_none_on_failure(tmp_path):
    with (
        patch("rmbackup._rmapi_get", return_value=None),
        patch("rmbackup.is_ecryptfs", return_value=False),
    ):
        result = rmbackup.fetch_file("/some/file", tmp_path)
    assert result is None


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


# ---------------------------------------------------------------------------
# rm_version — .rm lines-file version detection
# ---------------------------------------------------------------------------

def _write_rm(path: Path, version: int) -> Path:
    path.write_bytes(f"reMarkable .lines file, version={version}          ".encode() + b"\x00\x01")
    return path

def test_rm_version_returns_5_when_header_is_v5(tmp_path):
    # Given an .rm file with a version=5 lines header
    rm = _write_rm(tmp_path / "page.rm", 5)

    # When we read its version
    result = rmbackup.rm_version(rm)

    # Then it reports 5
    assert result == 5

def test_rm_version_returns_6_when_header_is_v6(tmp_path):
    # Given an .rm file with a version=6 lines header
    rm = _write_rm(tmp_path / "page.rm", 6)

    # When we read its version
    result = rmbackup.rm_version(rm)

    # Then it reports 6
    assert result == 6

def test_rm_version_returns_none_when_not_a_lines_file(tmp_path):
    # Given a file that is not a reMarkable lines file
    rm = tmp_path / "page.rm"
    rm.write_bytes(b"%PDF-1.4 not a lines file")

    # When we read its version
    result = rmbackup.rm_version(rm)

    # Then it reports None
    assert result is None


# ---------------------------------------------------------------------------
# page_order — ordered page UUIDs from a .content file
# ---------------------------------------------------------------------------

def test_page_order_reads_v6_cpages_ids(tmp_path):
    # Given a v6 .content file whose cPages.pages carry id dicts
    content = tmp_path / "doc.content"
    content.write_text(json.dumps({"cPages": {"pages": [{"id": "aaa"}, {"id": "bbb"}]}}))

    # When we read the page order
    result = rmbackup.page_order(content)

    # Then the ids come back in order
    assert result == ["aaa", "bbb"]

def test_page_order_reads_legacy_pages_list(tmp_path):
    # Given a legacy .content file with a flat pages list of UUID strings
    content = tmp_path / "doc.content"
    content.write_text(json.dumps({"pages": ["xxx", "yyy", "zzz"]}))

    # When we read the page order
    result = rmbackup.page_order(content)

    # Then the UUIDs come back in order
    assert result == ["xxx", "yyy", "zzz"]


# ---------------------------------------------------------------------------
# find_last_full_backup — newest dated backup dir under a data dir
# ---------------------------------------------------------------------------

def test_find_last_full_backup_returns_newest_when_multiple(tmp_path):
    # Given a data dir holding two dated backup subdirs
    (tmp_path / "remarkable-backup-20260101-000000").mkdir()
    (tmp_path / "remarkable-backup-20260517-214421").mkdir()

    # When we find the last full backup
    found = rmbackup.find_last_full_backup(tmp_path)

    # Then it returns the newest dir with its parsed UTC timestamp
    assert found is not None
    d, ts = found
    assert d.name == "remarkable-backup-20260517-214421"
    assert ts == datetime(2026, 5, 17, 21, 44, 21, tzinfo=timezone.utc)

def test_find_last_full_backup_returns_none_when_absent(tmp_path):
    # Given a data dir with no dated backup subdirs
    (tmp_path / "incremental-since-260517").mkdir()

    # When we find the last full backup
    found = rmbackup.find_last_full_backup(tmp_path)

    # Then there is nothing to return
    assert found is None


# ---------------------------------------------------------------------------
# order_rm_pages — select+order annotation layers, never dropping any
# ---------------------------------------------------------------------------

def test_order_rm_pages_orders_by_page_order_when_ids_match(tmp_path):
    # Given a page order and .rm layers whose uuids are a subset of it
    order = ["p1", "p2", "p3"]
    rm_ids = ["p3", "p1"]

    # When we order the annotation layers
    result = rmbackup.order_rm_pages(order, rm_ids)

    # Then they follow the document's page order
    assert result == ["p1", "p3"]

def test_order_rm_pages_keeps_layers_when_uuids_not_in_page_order(tmp_path):
    # Given .rm layers named by uuids absent from the page-id list (a real format variant)
    order = ["q1", "q2", "q3", "q4"]
    rm_ids = ["ann-b", "ann-a"]

    # When we order the annotation layers
    result = rmbackup.order_rm_pages(order, rm_ids)

    # Then no layer is dropped (they are kept, deterministically sorted)
    assert result == ["ann-a", "ann-b"]
