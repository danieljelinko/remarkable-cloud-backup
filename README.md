# reMarkable Cloud Backup

Backup created with [rmapi](https://github.com/ddvk/rmapi) (v0.0.33) on 2026-05-17.

## Contents

```
remarkable-backup-20260517-214421/   # 559 .rmdoc files, 2.2 GB
remarkable-manifest-root.txt         # top-level cloud listing
remarkable-manifest-recursive.txt    # full recursive cloud listing (636 entries)
```

## How to open .rmdoc files

`.rmdoc` files are ZIP archives. What's inside depends on the document type.

### Check what's in a file

```bash
unzip -l "SomeFile.rmdoc" | grep -E "\.pdf|\.epub|\.rm"
```

- `.pdf` or `.epub` → the original imported file is inside, extract it
- `.rm` files only → handwritten notebook, see below

### Imported PDFs and EPUBs

The original file is inside the archive:

```bash
# Extract just the PDF
unzip -p "SomeBook.rmdoc" "*.pdf" > SomeBook.pdf

# Or extract everything
unzip "SomeBook.rmdoc" -d SomeBook/
```

### Handwritten notebooks

Pages are stored as `.rm` binary vector files (not directly viewable). Options:

**1. Web export — best quality, no install needed**

Go to [my.remarkable.com](https://my.remarkable.com), open the notebook, and download as PDF.

**2. rmapi `geta` — built-in renderer (basic, limited pen support)**

```bash
printf 'geta /path/to/Notebook\nexit\n' | ~/.local/bin/rmapi
```

Produces a PDF in the current directory.

**3. `rmrl` — better rendering than `geta`**

```bash
pip install rmrl
rmrl SomeNotebook.rmdoc output.pdf
```

## Re-running the backup

The backup is a point-in-time snapshot. To take a fresh one without touching this copy:

```bash
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$HOME/Work/remarkable/cloud-backup/remarkable-backup-$STAMP"
mkdir -p "$OUT"
~/.local/bin/rmapi mget -o "$OUT" /
```

## Notes

- Files with very long names (> ~143 bytes) were truncated at the end due to the eCryptFS
  filename limit on this machine. The content is intact; only the tail of the filename is cut.
- `rmapi` is installed at `~/.local/bin/rmapi` and is already authenticated.
  The auth token lives at `~/.rmapi`.
