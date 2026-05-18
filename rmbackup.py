#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
reMarkable Cloud Backup — rmbackup.py

Importable library and standalone CLI.
Run directly with:  uv run rmbackup.py backup --out /path/to/output
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RMAPI_BIN = Path.home() / ".local" / "bin" / "rmapi"
GITHUB_RELEASES = "https://api.github.com/repos/ddvk/rmapi/releases/latest"
ECRYPTFS_SAFE_BYTES = 130   # empirically verified: eCryptFS limit ~143, use 130
NETWORK_RETRIES = 3
NETWORK_BACKOFF = 5         # seconds

# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------

def _arch_suffix() -> str:
    import platform
    m = platform.machine()
    return {"x86_64": "amd64", "aarch64": "arm64"}.get(m, m)


def install_rmapi(bin_dir: Path = RMAPI_BIN.parent) -> Path:
    """Download latest ddvk/rmapi release for this arch and install to bin_dir."""
    arch = _arch_suffix()
    print(f"Fetching latest rmapi release for linux-{arch}…")

    with urllib.request.urlopen(GITHUB_RELEASES, timeout=30) as r:
        data = json.loads(r.read())

    assets = data.get("assets", [])
    asset = next(
        (a for a in assets if f"linux-{arch}" in a["name"] and a["name"].endswith(".tar.gz")),
        None,
    )
    if not asset:
        raise RuntimeError(f"No linux-{arch} asset found in latest release")

    url = asset["browser_download_url"]
    print(f"Downloading {asset['name']} from {url}")

    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "rmapi.tar.gz"
        urllib.request.urlretrieve(url, archive)
        with tarfile.open(archive) as tf:
            tf.extractall(tmp)
        binary = next(Path(tmp).rglob("rmapi"), None)
        if not binary:
            raise RuntimeError("rmapi binary not found in archive")
        bin_dir.mkdir(parents=True, exist_ok=True)
        dest = bin_dir / "rmapi"
        shutil.copy2(binary, dest)
        dest.chmod(0o755)

    print(f"Installed rmapi to {dest}")
    _ensure_path(bin_dir)
    return dest


def _ensure_path(bin_dir: Path) -> None:
    path_dirs = os.environ.get("PATH", "").split(":")
    if str(bin_dir) not in path_dirs:
        os.environ["PATH"] = f"{bin_dir}:{os.environ.get('PATH', '')}"


# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------

def check_rmapi() -> bool:
    """Return True if rmapi is on PATH."""
    _ensure_path(RMAPI_BIN.parent)
    return shutil.which("rmapi") is not None


def rmapi_path() -> str:
    _ensure_path(RMAPI_BIN.parent)
    p = shutil.which("rmapi")
    if not p:
        raise RuntimeError("rmapi not found — run: uv run rmbackup.py install")
    return p


def check_auth() -> bool:
    """Return True if rmapi is already authenticated (can list cloud root)."""
    try:
        r = subprocess.run(
            [rmapi_path(), "ls", "/"],
            capture_output=True, timeout=30,
        )
        return r.returncode == 0
    except Exception:
        return False


def check_prerequisites() -> dict:
    """Return dict with 'rmapi' and 'auth' boolean fields."""
    has_rmapi = check_rmapi()
    has_auth = check_auth() if has_rmapi else False
    return {"rmapi": has_rmapi, "auth": has_auth}


# ---------------------------------------------------------------------------
# eCryptFS detection and filename handling
# ---------------------------------------------------------------------------

def is_ecryptfs(path: str | Path) -> bool:
    """Return True if path lives on an eCryptFS mount."""
    target = str(Path(path).resolve())
    try:
        mounts = Path("/proc/mounts").read_text()
    except OSError:
        return False
    for line in mounts.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "ecryptfs":
            mount_point = parts[1]
            if target.startswith(mount_point):
                return True
    return False


def truncate_utf8(s: str, max_bytes: int) -> str:
    """Truncate string so its UTF-8 encoding is at most max_bytes bytes."""
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return s
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def safe_filename(name: str, ecryptfs: bool) -> str:
    """Return a filename safe for the target filesystem."""
    if ecryptfs:
        return truncate_utf8(name, ECRYPTFS_SAFE_BYTES)
    return name


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def get_manifest(out_path: Path) -> int:
    """Run `rmapi find /`, write to out_path. Return line count."""
    result = subprocess.run(
        [rmapi_path(), "find", "/"],
        capture_output=True, text=True, timeout=120,
    )
    out_path.write_text(result.stdout)
    lines = [l for l in result.stdout.splitlines() if l.startswith("[f]")]
    return len(lines)


# ---------------------------------------------------------------------------
# mget — bulk download
# ---------------------------------------------------------------------------

def run_mget(out_dir: Path, schema: int | None = None, concurrency: int | None = None) -> str:
    """
    Run `rmapi mget -o out_dir /`. Return combined stdout+stderr.
    Raises RuntimeError if rmapi is not installed.
    """
    env = os.environ.copy()
    if schema is not None:
        env["RMAPI_FORCE_SCHEMA_VERSION"] = str(schema)
    if concurrency is not None:
        env["RMAPI_CONCURRENT"] = str(concurrency)

    out_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [rmapi_path(), "mget", "-o", str(out_dir), "/"],
        capture_output=True, text=True, env=env, timeout=7200,
    )
    return proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# Error parsing
# ---------------------------------------------------------------------------

_RE_LONG_NAME = re.compile(
    r"failed to copy \S+ to (.+?), er:.*file name too long", re.MULTILINE
)
_RE_NETWORK = re.compile(
    r"downloading \[(.+?)\]\.\.\.(ERROR:.*?(?:connection reset|http request failed))",
    re.MULTILINE | re.DOTALL,
)
_RE_SCHEMA = re.compile(r"schema|root.?index", re.IGNORECASE)


def parse_errors(mget_output: str) -> dict:
    """
    Parse mget output. Returns:
      {
        "long_name":  [local_path, …],   # file name too long
        "network":    [local_path, …],   # connection reset / http error
        "schema":     bool,              # schema/root-index error seen
      }
    """
    long_name = list(dict.fromkeys(_RE_LONG_NAME.findall(mget_output)))
    network_raw = _RE_NETWORK.findall(mget_output)
    network = list(dict.fromkeys(p for p, _ in network_raw))
    schema = bool(_RE_SCHEMA.search(mget_output))
    return {"long_name": long_name, "network": network, "schema": schema}


# ---------------------------------------------------------------------------
# Fixing long-name failures (eCryptFS workaround)
# ---------------------------------------------------------------------------

def _rmapi_get(cloud_path: str, dest_dir: Path) -> Path | None:
    """
    Download a single cloud file to dest_dir via rmapi interactive shell.
    Returns the Path of the downloaded file, or None on failure.
    """
    before = set(dest_dir.glob("*.rmdoc"))
    cmd = f'get "{cloud_path}"\nexit\n'
    subprocess.run(
        [rmapi_path()],
        input=cmd, capture_output=True, text=True,
        cwd=str(dest_dir), timeout=300,
    )
    after = set(dest_dir.glob("*.rmdoc"))
    new_files = after - before
    return next(iter(new_files), None)


def fix_long_filenames(
    local_paths: list[str],
    backup_dir: Path,
    tmp_dir: Path,
) -> tuple[int, list[str]]:
    """
    For each failed local path:
      1. Derive cloud path (strip backup_dir prefix + .rmdoc suffix)
      2. Download to tmp_dir (no eCryptFS limit there)
      3. Move to backup_dir with truncated filename
    Returns (success_count, still_failed_paths).
    """
    ok = 0
    failed = []
    seen_cloud: dict[str, Path] = {}   # cloud_path -> already-downloaded file

    for local_path_str in local_paths:
        local_path = Path(local_path_str)
        cloud_path = str(local_path).replace(str(backup_dir), "", 1).removesuffix(".rmdoc")
        cloud_base = Path(cloud_path).name
        cloud_dir = str(Path(cloud_path).parent)
        local_dir = backup_dir / cloud_dir.lstrip("/")
        local_dir.mkdir(parents=True, exist_ok=True)

        safe_base = truncate_utf8(cloud_base, ECRYPTFS_SAFE_BYTES - 6)  # room for .rmdoc
        target = local_dir / f"{safe_base}.rmdoc"

        if target.exists():
            ok += 1
            continue

        # Reuse already-downloaded tmp file for same cloud path
        if cloud_path in seen_cloud and seen_cloud[cloud_path].exists():
            shutil.copy2(seen_cloud[cloud_path], target)
            ok += 1
            continue

        downloaded = _rmapi_get(cloud_path, tmp_dir)
        if downloaded:
            seen_cloud[cloud_path] = downloaded
            shutil.copy2(downloaded, target)
            ok += 1
        else:
            failed.append(local_path_str)

    return ok, failed


# ---------------------------------------------------------------------------
# Retrying network failures
# ---------------------------------------------------------------------------

def retry_network_errors(
    local_paths: list[str],
    backup_dir: Path,
    tmp_dir: Path,
) -> tuple[int, list[str]]:
    """Retry network-failed files with exponential back-off."""
    ok = 0
    failed = []

    for local_path_str in local_paths:
        local_path = Path(local_path_str)
        if local_path.exists():
            ok += 1
            continue

        cloud_path = str(local_path).replace(str(backup_dir), "", 1).removesuffix(".rmdoc")
        local_dir = local_path.parent
        local_dir.mkdir(parents=True, exist_ok=True)

        success = False
        for attempt in range(1, NETWORK_RETRIES + 1):
            downloaded = _rmapi_get(cloud_path, tmp_dir)
            if downloaded:
                shutil.move(str(downloaded), str(local_path))
                success = True
                break
            if attempt < NETWORK_RETRIES:
                time.sleep(NETWORK_BACKOFF * attempt)

        if success:
            ok += 1
        else:
            failed.append(local_path_str)

    return ok, failed


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_backup(backup_dir: Path, manifest_path: Path | None = None) -> dict:
    """
    Count downloaded files and compare with manifest.
    Returns dict with status, file_count, manifest_count, size_bytes.
    """
    files = list(backup_dir.rglob("*.rmdoc"))
    file_count = len(files)
    size_bytes = sum(f.stat().st_size for f in files)

    manifest_count = None
    if manifest_path and manifest_path.exists():
        lines = [l for l in manifest_path.read_text().splitlines() if l.startswith("[f]")]
        manifest_count = len(lines)

    status = "ok"
    if file_count == 0:
        status = "empty"
    elif manifest_count is not None and file_count < manifest_count * 0.5:
        status = "suspicious"

    return {
        "status": status,
        "file_count": file_count,
        "manifest_count": manifest_count,
        "size_bytes": size_bytes,
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def full_backup(out_dir: Path, schema: int | None = None) -> dict:
    """
    Full backup flow:
      1. Manifest snapshot
      2. mget (with optional schema override / auto-retry)
      3. Fix long filenames (eCryptFS workaround)
      4. Retry network failures
      5. Verify

    Returns a report dict.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = out_dir / f"remarkable-backup-{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = out_dir / f"manifest-{stamp}.txt"
    print("Step 1/5 — capturing cloud manifest…")
    manifest_count = get_manifest(manifest_path)
    print(f"  {manifest_count} files in cloud")

    if manifest_count == 0:
        raise RuntimeError("Cloud manifest is empty — aborting to avoid capturing nothing")

    print(f"Step 2/5 — bulk download to {backup_dir} …")
    ecryptfs = is_ecryptfs(backup_dir)
    if ecryptfs:
        print("  eCryptFS detected — long filenames will be fixed after main pass")

    schemas_to_try = [schema] if schema else [None, 4, 3]
    errors = {}
    for s in schemas_to_try:
        mget_output = run_mget(backup_dir, schema=s)
        errors = parse_errors(mget_output)
        if not errors["schema"]:
            break
        print(f"  Schema error with version={s}, trying next…")

    with tempfile.TemporaryDirectory(prefix="rmapi_fix_") as tmp:
        tmp_dir = Path(tmp)

        print(f"Step 3/5 — fixing {len(errors['long_name'])} long-filename failures…")
        ln_ok, ln_fail = (0, [])
        if errors["long_name"]:
            ln_ok, ln_fail = fix_long_filenames(errors["long_name"], backup_dir, tmp_dir)
            print(f"  fixed {ln_ok}, still failed {len(ln_fail)}")

        print(f"Step 4/5 — retrying {len(errors['network'])} network failures…")
        net_ok, net_fail = (0, [])
        if errors["network"]:
            net_ok, net_fail = retry_network_errors(errors["network"], backup_dir, tmp_dir)
            print(f"  recovered {net_ok}, still failed {len(net_fail)}")

    print("Step 5/5 — verifying backup…")
    report = verify_backup(backup_dir, manifest_path)
    report.update({
        "backup_dir": str(backup_dir),
        "manifest_path": str(manifest_path),
        "long_name_fixed": ln_ok,
        "long_name_failed": ln_fail,
        "network_recovered": net_ok,
        "network_failed": net_fail,
        "ecryptfs": ecryptfs,
    })

    if report["status"] == "empty":
        raise RuntimeError(f"Backup is empty — check rmapi authentication and cloud content")

    return report


def _print_report(report: dict) -> None:
    print("\n" + "=" * 60)
    print("BACKUP REPORT")
    print("=" * 60)
    print(f"Directory : {report['backup_dir']}")
    print(f"Manifest  : {report['manifest_path']}")
    size_mb = report["size_bytes"] / 1024 / 1024
    print(f"Files     : {report['file_count']} downloaded  ({size_mb:.0f} MB)")
    if report["manifest_count"]:
        print(f"Cloud     : {report['manifest_count']} items in manifest")
    print(f"eCryptFS  : {'yes — filenames truncated to 130 bytes' if report['ecryptfs'] else 'no'}")
    print(f"Long-name : {report['long_name_fixed']} fixed, {len(report['long_name_failed'])} failed")
    print(f"Network   : {report['network_recovered']} recovered, {len(report['network_failed'])} failed")
    print(f"Status    : {report['status'].upper()}")
    if report["long_name_failed"] or report["network_failed"]:
        print("\nStill-failed files:")
        for p in report["long_name_failed"] + report["network_failed"]:
            print(f"  {p}")
    print("=" * 60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cmd_install(args: argparse.Namespace) -> int:
    try:
        install_rmapi()
        return 0
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


def _cmd_check(args: argparse.Namespace) -> int:
    prereqs = check_prerequisites()
    print(f"rmapi installed : {'yes (' + rmapi_path() + ')' if prereqs['rmapi'] else 'NO — run: uv run rmbackup.py install'}")
    print(f"authenticated   : {'yes' if prereqs['auth'] else 'NO — run rmapi to authenticate'}")
    return 0 if all(prereqs.values()) else 1


def _cmd_backup(args: argparse.Namespace) -> int:
    if not check_rmapi():
        print("rmapi not found — run: uv run rmbackup.py install", file=sys.stderr)
        return 1
    if not check_auth():
        print("Not authenticated. Start rmapi and enter your one-time code from:", file=sys.stderr)
        print("  https://my.remarkable.com/device/browser?showOtp=true", file=sys.stderr)
        return 1

    out_dir = Path(args.out).expanduser().resolve()
    try:
        report = full_backup(out_dir, schema=args.schema)
        _print_report(report)
        return 0 if report["status"] == "ok" else 1
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


def _cmd_verify(args: argparse.Namespace) -> int:
    d = Path(args.dir).expanduser().resolve()
    manifest = Path(args.manifest).expanduser().resolve() if args.manifest else None
    report = verify_backup(d, manifest)
    size_mb = report["size_bytes"] / 1024 / 1024
    print(f"files    : {report['file_count']}")
    if report["manifest_count"] is not None:
        print(f"manifest : {report['manifest_count']}")
    print(f"size     : {size_mb:.0f} MB")
    print(f"status   : {report['status']}")
    return 0 if report["status"] == "ok" else 1


def _cmd_manifest(args: argparse.Namespace) -> int:
    out = Path(args.out).expanduser().resolve() if args.out else Path(f"manifest-{time.strftime('%Y%m%d')}.txt")
    count = get_manifest(out)
    print(f"Saved {count} file entries to {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="reMarkable cloud backup tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("install", help="Install rmapi binary to ~/.local/bin")

    sub.add_parser("check", help="Check prerequisites (rmapi installed + authenticated)")

    p_backup = sub.add_parser("backup", help="Run a full backup")
    p_backup.add_argument("--out", required=True, help="Output directory (backup created inside)")
    p_backup.add_argument("--schema", type=int, choices=[3, 4], help="Force sync schema version")

    p_verify = sub.add_parser("verify", help="Verify an existing backup directory")
    p_verify.add_argument("--dir", required=True, help="Backup directory to verify")
    p_verify.add_argument("--manifest", help="Manifest file to compare against")

    p_manifest = sub.add_parser("manifest", help="Capture cloud manifest only")
    p_manifest.add_argument("--out", help="Output file path")

    args = parser.parse_args()
    dispatch = {
        "install": _cmd_install,
        "check": _cmd_check,
        "backup": _cmd_backup,
        "verify": _cmd_verify,
        "manifest": _cmd_manifest,
    }
    return dispatch[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
