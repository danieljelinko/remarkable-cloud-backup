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
import zipfile
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
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
# Fetch single file
# ---------------------------------------------------------------------------

def fetch_file(cloud_path: str, out_dir: Path) -> Path | None:
    """
    Download a single file from the reMarkable cloud to out_dir.
    Handles eCryptFS filename truncation automatically.
    Returns the local Path on success, None on failure.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    ecryptfs = is_ecryptfs(out_dir)

    with tempfile.TemporaryDirectory(prefix="rmapi_fetch_") as tmp:
        tmp_dir = Path(tmp)
        downloaded = _rmapi_get(cloud_path, tmp_dir)
        if not downloaded:
            return None

        if ecryptfs:
            stem = truncate_utf8(downloaded.stem, ECRYPTFS_SAFE_BYTES - 6)
            name = f"{stem}.rmdoc"
        else:
            name = downloaded.name

        dest = out_dir / name
        shutil.move(str(downloaded), str(dest))
        return dest


# ---------------------------------------------------------------------------
# List files with timestamps (for "recent" queries)
# ---------------------------------------------------------------------------

def ls_json(cloud_dir: str) -> list[dict]:
    """
    Run `rmapi -json ls <cloud_dir>`. Returns list of entry dicts.
    Each dict has at minimum: VissibleName, Type, ModifiedClient.
    Returns [] on error or auth failure.
    """
    result = subprocess.run(
        [rmapi_path(), "-json", "ls", cloud_dir],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def list_recent(days: int = 7, root: str = "/", max_workers: int = 8) -> list[dict]:
    """
    Return cloud file entries modified within the last N days, newest first.

    Strategy: one `rmapi find` call to enumerate directories, then one
    `rmapi -json ls <dir>` per unique directory in parallel — O(dirs) not O(files).

    Each returned dict contains:
      _cloud_path  : full cloud path string
      _modified_dt : datetime (UTC)
      VissibleName : display name from cloud
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Get all file paths to discover which directories exist
    find_result = subprocess.run(
        [rmapi_path(), "find", root],
        capture_output=True, text=True, timeout=120,
    )
    dirs: set[str] = set()
    for line in find_result.stdout.splitlines():
        if line.startswith("[f] "):
            parent = str(Path(line[4:]).parent)
            dirs.add("/" if parent in (".", "/") else parent)

    if not dirs:
        return []

    recent: list[dict] = []

    def fetch_dir(d: str) -> list[dict]:
        entries = ls_json(d)
        results = []
        for e in entries:
            if e.get("Type") != "DocumentType":
                continue
            ts_str = e.get("ModifiedClient", "")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str.rstrip("Z")).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if ts >= cutoff:
                e["_modified_dt"] = ts
                name = e.get("VissibleName", "")
                e["_cloud_path"] = f"{d.rstrip('/')}/{name}"
                results.append(e)
        return results

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(fetch_dir, d) for d in dirs]
        for future in as_completed(futures):
            recent.extend(future.result())

    return sorted(recent, key=lambda x: x["_modified_dt"], reverse=True)


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
# Inspect — prepare a .rmdoc for reading by Claude Code
# ---------------------------------------------------------------------------

def _pdf_to_text(pdf_path: Path) -> str:
    """Extract embedded text from a PDF using pdftotext."""
    r = subprocess.run(
        ["pdftotext", str(pdf_path), "-"],
        capture_output=True, text=True, timeout=60,
    )
    return r.stdout.strip()


def _rmc_to_markdown(rm_files: list[Path]) -> str:
    """Convert .rm files to markdown using rmc (typed text / highlights only)."""
    parts = []
    for rm in rm_files:
        r = subprocess.run(
            ["rmc", "-f", "rm", "-t", "markdown", str(rm)],
            capture_output=True, text=True, timeout=30,
        )
        text = r.stdout.strip()
        if text and text not in ("# Highlights", "#"):
            parts.append(text)
    return "\n\n---\n\n".join(parts)


def _geta_rendered_pdf(cloud_path: str, dest_dir: Path) -> Path | None:
    """Download a cloud-rendered PDF via `rmapi geta` (fallback for unsupported .rm versions)."""
    before = set(dest_dir.glob("*.pdf"))
    subprocess.run(
        [rmapi_path()],
        input=f'geta "{cloud_path}"\nexit\n',
        capture_output=True, text=True,
        cwd=str(dest_dir), timeout=300,
    )
    after = set(dest_dir.glob("*.pdf"))
    return next(iter(after - before), None)


def _pdf_to_images(pdf_path: Path, out_dir: Path, dpi: int = 150) -> list[Path]:
    """Rasterise a PDF to PNG images (one per page) using pdftoppm."""
    prefix = out_dir / "page"
    subprocess.run(
        ["pdftoppm", "-r", str(dpi), "-png", str(pdf_path), str(prefix)],
        capture_output=True, timeout=120,
    )
    return sorted(out_dir.glob("page-*.png"))


def _rm_to_png(rm_path: Path, out_path: Path, dpi: int = 150) -> bool:
    """
    Render a single .rm file to PNG locally:
      .rm → SVG (rmc) → PNG (ImageMagick convert)
    Returns True on success.
    """
    svg_path = out_path.with_suffix(".svg")

    r = subprocess.run(
        ["rmc", "-f", "rm", "-t", "svg", str(rm_path), "-o", str(svg_path)],
        capture_output=True, timeout=30,
    )
    if r.returncode != 0 or not svg_path.exists() or svg_path.stat().st_size < 200:
        return False

    r2 = subprocess.run(
        ["convert", "-density", str(dpi), "-background", "white",
         "-flatten", str(svg_path), str(out_path)],
        capture_output=True, timeout=60,
    )
    svg_path.unlink(missing_ok=True)
    return r2.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0


def prepare_inspect(
    rmdoc_path: Path,
    out_dir: Path,
    cloud_path: str | None = None,
    dpi: int = 150,
) -> dict:
    """
    Prepare a .rmdoc for inspection by Claude Code.

    Rendering priority for handwritten notebooks:
      1. Local: rmc → SVG → ImageMagick PNG  (no auth, no cloud)
      2. Cloud fallback: rmapi geta → pdftoppm PNG  (needs auth)

    Returns:
      {
        "type":      "text" | "images" | "mixed",
        "text":      str | None,
        "text_file": str | None,
        "images":    [str, ...],
        "rmc_text":  str | None,
        "render":    "local" | "cloud" | "none",
      }
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="rminspect_") as tmp:
        tmp_dir = Path(tmp)
        with zipfile.ZipFile(rmdoc_path) as zf:
            zf.extractall(tmp_dir)

        pdf_files = list(tmp_dir.rglob("*.pdf"))
        rm_files  = sorted(tmp_dir.rglob("*.rm"))

        result: dict = {
            "text": None, "text_file": None,
            "images": [], "rmc_text": None, "render": "none",
        }

        # --- Embedded PDF: text extraction ---
        pdf_text = _pdf_to_text(pdf_files[0]) if pdf_files else ""
        if len(pdf_text) > 100:
            text_file = out_dir / "text.txt"
            text_file.write_text(pdf_text)
            result["text"] = pdf_text
            result["text_file"] = str(text_file)

        # --- .rm files: typed/highlighted content via rmc ---
        rmc_text = _rmc_to_markdown(rm_files) if rm_files else ""
        if rmc_text:
            result["rmc_text"] = rmc_text

        # --- Handwriting rendering (only if no good embedded text) ---
        if len(pdf_text) < 100 and rm_files:
            images: list[Path] = []

            # Path 1 — local render: rmc SVG → ImageMagick PNG
            print("  Rendering pages locally (rmc → SVG → PNG)…", flush=True)
            for i, rm in enumerate(rm_files, 1):
                out_png = out_dir / f"page-{i:03d}.png"
                if _rm_to_png(rm, out_png, dpi=dpi):
                    images.append(out_png)

            if images:
                result["render"] = "local"
            else:
                # Path 2 — cloud fallback: geta → pdftoppm
                print("  Local render failed, trying cloud geta…", flush=True)
                if cloud_path and check_auth():
                    source_pdf = _geta_rendered_pdf(cloud_path, tmp_dir)
                    if source_pdf:
                        images = _pdf_to_images(source_pdf, out_dir, dpi=dpi)
                        if images:
                            result["render"] = "cloud"
                elif pdf_files:
                    # Last resort: render embedded PDF (annotations only)
                    images = _pdf_to_images(pdf_files[0], out_dir, dpi=dpi)
                    if images:
                        result["render"] = "local"

            result["images"] = [str(p) for p in images]

        result["type"] = (
            "mixed"  if (result["text"] and result["images"]) else
            "text"   if result["text"] else
            "images" if result["images"] else
            "none"
        )
        return result


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


def _cmd_inspect(args: argparse.Namespace) -> int:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out_dir) if args.out_dir else Path(tempfile.mkdtemp(prefix=f"rminspect_{stamp}_"))

    cloud_path: str | None = None
    rmdoc_path: Path | None = None

    candidate = Path(args.path)
    if candidate.suffix == ".rmdoc" and candidate.exists():
        rmdoc_path = candidate
    else:
        cloud_path = args.path
        print(f"Fetching {cloud_path!r}…")
        fetch_dir = Path(tempfile.mkdtemp(prefix="rmfetch_"))
        downloaded = _rmapi_get(cloud_path, fetch_dir)
        if not downloaded:
            print(f"ERROR: could not fetch {cloud_path!r}", file=sys.stderr)
            return 1
        rmdoc_path = downloaded

    print(f"Preparing content in {out_dir} …")
    result = prepare_inspect(rmdoc_path, out_dir, cloud_path=cloud_path)

    # Print a machine-readable + human summary
    print(json.dumps(result, indent=2))
    print()
    if result["text"]:
        print(f"Embedded text saved to: {result['text_file']}")
    if result["rmc_text"]:
        print("Typed/highlighted content (rmc):")
        print(result["rmc_text"])
    if result["images"]:
        print(f"{len(result['images'])} page image(s) ready for Claude Code to read:")
        for p in result["images"]:
            print(f"  {p}")
    return 0


def _cmd_fetch(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).expanduser().resolve()
    print(f"Fetching: {args.path}")
    result = fetch_file(args.path, out_dir)
    if result:
        print(f"Saved: {result}")
        return 0
    print(f"ERROR: could not download {args.path!r}", file=sys.stderr)
    return 1


def _cmd_recent(args: argparse.Namespace) -> int:
    print(f"Scanning for files modified in the last {args.days} day(s)…")
    entries = list_recent(days=args.days)

    if not entries:
        print("No recently modified files found.")
        return 0

    print(f"\nFound {len(entries)} file(s):\n")
    for e in entries:
        ts = e["_modified_dt"].strftime("%Y-%m-%d %H:%M UTC")
        print(f"  {ts}  {e['_cloud_path']}")

    if args.download:
        out_dir = Path(args.out).expanduser().resolve()
        print(f"\nDownloading to {out_dir}…")
        ok = 0
        for e in entries:
            saved = fetch_file(e["_cloud_path"], out_dir)
            if saved:
                print(f"  OK  {saved.name}")
                ok += 1
            else:
                print(f"  FAIL  {e['_cloud_path']}", file=sys.stderr)
        print(f"\nDownloaded {ok}/{len(entries)}")

    return 0


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

    p_fetch = sub.add_parser("fetch", help="Download a single file by cloud path")
    p_fetch.add_argument("path", help="Cloud path (e.g. /Work/Meeting notes)")
    p_fetch.add_argument("--out", default=".", help="Local output directory (default: current dir)")

    p_recent = sub.add_parser("recent", help="List (and optionally download) recently modified files")
    p_recent.add_argument("--days", type=int, default=7, help="Look back N days (default: 7)")
    p_recent.add_argument("--download", action="store_true", help="Download the listed files")
    p_recent.add_argument("--out", default=".", help="Download destination (default: current dir)")

    p_inspect = sub.add_parser(
        "inspect",
        help="Prepare a notebook for text extraction (renders pages for Claude Code to read)",
    )
    p_inspect.add_argument("path", help="Cloud path or local .rmdoc file")
    p_inspect.add_argument(
        "--out-dir", help="Directory to save rendered images / text (default: auto temp dir)"
    )

    args = parser.parse_args()
    dispatch = {
        "install": _cmd_install,
        "check": _cmd_check,
        "backup": _cmd_backup,
        "verify": _cmd_verify,
        "manifest": _cmd_manifest,
        "fetch": _cmd_fetch,
        "recent": _cmd_recent,
        "inspect": _cmd_inspect,
    }
    return dispatch[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
