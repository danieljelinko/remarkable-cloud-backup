# reMarkable Cloud Backup

Backup, fetch, and read your reMarkable cloud library from Linux.
Handles eCryptFS filename limits, network retries, and schema version mismatches automatically.
Designed for use with [Claude Code](https://claude.ai/code) via the bundled skill.

## Features

- **Full backup** — download your entire library as `.rmdoc` files
- **Fetch** — grab a single file by cloud path
- **Recent** — list files modified in the last N days
- **Inspect** — render any notebook to text or images for AI reading:
  - Imported PDFs → embedded text extracted with `pdftotext`
  - Typed/highlighted content → extracted with `rmc`
  - Handwritten notebooks → rendered locally via `rmc → SVG → ImageMagick PNG` (no cloud needed)
  - Cloud fallback → `rmapi geta` if local render fails

## System requirements

Install these once:

```bash
# Poppler (pdftotext + pdftoppm) and ImageMagick (convert)
sudo apt install -y poppler-utils imagemagick

# Build headers needed for rmrl/reportlab (optional, for rmc PDF output)
sudo apt install -y libfreetype-dev libjpeg-dev zlib1g-dev
```

## Python setup

Requires [uv](https://github.com/astral-sh/uv).

```bash
git clone https://github.com/danieljelinko/remarkable-cloud-backup
cd remarkable-cloud-backup
uv sync
```

Install CLI tools:

```bash
uv tool install rmc --python 3.12   # local .rm → SVG/markdown/PDF
```

Install rmapi (the reMarkable cloud client):

```bash
just install   # downloads the latest ddvk/rmapi binary to ~/.local/bin
```

## Authentication

rmapi needs a one-time pairing code. Tokens last about 5 days.

1. Open in a browser signed in to your reMarkable account:
   `https://my.remarkable.com/device/browser?showOtp=true`
2. Paste the 8-character code when prompted (or pipe it directly):
   ```bash
   echo "<CODE>" | ~/.local/bin/rmapi ls /
   ```
3. Confirm the listing is non-empty.

Re-authenticate with the same steps when the token expires.

## Usage

All commands are available via `just`:

```bash
just install           # install rmapi binary
just check             # verify rmapi is installed and authenticated

just backup            # full backup → ../260517_remarkable_data_backup/
just verify            # verify most recent backup

just recent            # list files modified in last 7 days
just recent days=14    # last 14 days

just fetch "/Work/Meeting notes"              # download a single .rmdoc
just inspect "/Work/Meeting notes"            # render for text extraction
just inspect "/path/to/local/file.rmdoc"      # works on local files too

just manifest          # capture cloud manifest only
just test              # run test suite
```

Or call the script directly (self-contained via PEP 723, no install needed):

```bash
uv run rmbackup.py backup --out ~/my-backup
uv run rmbackup.py inspect "/Work/Meeting notes"
uv run rmbackup.py recent --days 7 --download --out ~/downloads
```

## Opening .rmdoc files

`.rmdoc` files are ZIP archives.

```bash
unzip -l "SomeFile.rmdoc"   # see what's inside
```

| Contains | Meaning | How to read |
|---|---|---|
| `.pdf` | Imported PDF | `unzip -p file.rmdoc "*.pdf" > out.pdf` |
| `.epub` | Imported EPUB | extract and open with any EPUB reader |
| `.rm` only | Handwritten notebook | `just inspect` (renders to PNG for Claude Code) |

For highest-quality handwriting PDF: go to [my.remarkable.com](https://my.remarkable.com), open the notebook, Download as PDF.

## Backup data location

Backed-up data lives **outside this repo** at `../260517_remarkable_data_backup/` by default.
Set `RM_BACKUP_DIR` to override:

```bash
export RM_BACKUP_DIR=/mnt/external/remarkable
just backup
```

Each run creates a new timestamped subdirectory — no previous backup is ever overwritten.

## Re-running a backup

```bash
just backup   # creates remarkable-backup-YYYYMMDD-HHMMSS/ inside RM_BACKUP_DIR
```

## Claude Code skill

The bundled skill (`~/.claude/skills/rm-backup/`) lets Claude Code run this workflow conversationally:

> "Show me my 10 most recently modified reMarkable docs"  
> "Fetch #3 and give me the text"

Claude Code handles the list → pick → render → read pipeline automatically.

## Architecture

```
rmbackup.py          standalone Python script (PEP 723, zero runtime deps)
├── install          download rmapi binary
├── check            verify prerequisites
├── backup           full mget + eCryptFS fix + network retry + verify
├── fetch            single file download
├── recent           list recently modified (parallel rmapi -json ls)
├── inspect          render .rmdoc for text extraction
│   ├── pdftotext    embedded PDF text (fast, free)
│   ├── rmc          typed/highlighted content from .rm files
│   └── rmc+convert  local handwriting render (SVG → PNG, no cloud needed)
├── verify           count files vs manifest
└── manifest         capture cloud file listing

tests/               30 unit tests, no network calls (pytest)
justfile             human-friendly recipes
```
