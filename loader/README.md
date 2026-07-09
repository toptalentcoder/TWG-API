# TWG Loader

Streams the per-state property TSV files into one Supabase Postgres table.

- **Swappable source:** `LocalSource` (default) reads `TWG_LOCAL_DIR`; `SFTPSource` /
  `GCSSource` seams are in `loader.py` — only the source class changes, the COPY path is identical.
- **Idempotent:** upsert on `(fips, propertyid)` — safe to re-run.
- **Streaming COPY:** ~8 MB peak memory even for the 1.8 GB Arkansas file.
- **Lowercases** all 229 column names; creates the composite PK and the `(situsstate, situszip5)` index.

## Usage (PowerShell)

```powershell
pip install -r requirements.txt
# Set SUPABASE_DSN first (Session pooler, port 5432) — see ../.env.example
$env:SUPABASE_DSN = "postgresql://postgres.<ref>:<pw>@<pooler-host>:5432/postgres?sslmode=require"

python loader.py --list          # list files, no DB connection
python loader.py --only DC       # load one state (start here)
python loader.py                 # load all, smallest-first (AR last)
```

See `../docs/RUNBOOK.md` for the full step-by-step guide.
