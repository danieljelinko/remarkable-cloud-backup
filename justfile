# reMarkable Cloud Backup
# Backed-up data lives outside this repo (sibling directory by default)
data_dir := env_var_or_default("RM_BACKUP_DIR", "../260517_remarkable_data_backup")

# Install rmapi binary to ~/.local/bin
install:
    uv run rmbackup.py install

# Check prerequisites (rmapi installed + authenticated)
check:
    uv run rmbackup.py check

# Run a full backup into data_dir
backup:
    uv run rmbackup.py backup --out {{data_dir}}

# Verify the most recent backup inside data_dir
verify:
    uv run rmbackup.py verify --dir {{data_dir}}

# Capture cloud manifest only
manifest:
    uv run rmbackup.py manifest --out {{data_dir}}/manifest-`date +%Y%m%d`.txt

# Fetch a single file by cloud path (quote paths with spaces)
# Example: just fetch "/Work/Meeting notes"
fetch path out=".":
    uv run rmbackup.py fetch "{{path}}" --out "{{out}}"

# List files modified in the last N days (default 7)
recent days="7":
    uv run rmbackup.py recent --days {{days}}

# List and download files modified in the last N days
recent-download days="7" out=".":
    uv run rmbackup.py recent --days {{days}} --out "{{out}}" --download

# Run test suite
test:
    uv run pytest tests/ -v

# Show CLI help
help:
    uv run rmbackup.py --help
