# TWG Property Data API — Build Guide (from scratch)

A complete, beginner-friendly walkthrough of everything we built: load The Warren
Group's per-state property files into one Supabase Postgres table and expose a fast,
filterable REST endpoint. Written so you can reproduce it from zero — including every
error we hit and how we fixed it.

**Assumes:** Windows 11, PowerShell. No prior Python or Supabase experience needed.

---

## 0. What you're building (the big picture)

```
  FTP server (state .txt files)
        │   1. download with an SFTP client (WinSCP/FileZilla)
        ▼
  Your PC  ──►  Python loader  ──►  Supabase Postgres (one table: public.properties)
                (loader.py)              │
                                         │  2. a Postgres function (search_properties)
                                         ▼
                              Supabase Edge Function (property-search)
                                         │   3. deployed with the Supabase CLI
                                         ▼
                              Customers call:  GET /property-search?state=DC&...
```

Three moving parts:
1. **Loader** (Python) — pulls the files, loads them into one Postgres table.
2. **Database** (Supabase Postgres) — stores the data + a search function.
3. **API** (Supabase Edge Function) — the public endpoint customers call.

---

## 1. Prerequisites — install these once

You likely already have **Git** and **Python 3** (check with `git --version` and
`python --version`). Install the rest:

| Tool | What it's for | Install (PowerShell) |
|---|---|---|
| **Python 3.12+** | Runs the loader | `winget install Python.Python.3.12` (3.14 also works) |
| **WinSCP** | Download files from the FTP/SFTP server | `winget install WinSCP.WinSCP` (or https://winscp.net) |
| **Supabase CLI** | Deploy the API endpoint | via **scoop** — see below |
| **Git** | Version control | `winget install Git.Git` |

Install the Supabase CLI via **scoop** (its recommended Windows method — do **not** use `npm`):
```powershell
irm get.scoop.sh | iex
scoop install supabase
supabase --version        # confirm it works
```

> ⚠️ **Avoid FileZilla's default installer** — it bundles adware (antivirus flags it as
> a PUA/bundler). If you must use FileZilla, click **Decline** on every offer screen.
> **WinSCP is clean** and does the same job.

---

## 2. Project structure

Create a version-controlled project. Keep the **code** separate from **data** and
**reference docs** so secrets and big files never end up in Git.

```
twg-api/                     ← the git repo (only this is version-controlled)
├── .env                     ← secrets (gitignored — NEVER commit)
├── .env.example             ← template with placeholders (safe to commit)
├── .gitignore
├── README.md
├── loader/
│   ├── loader.py            ← the data loader
│   └── requirements.txt     ← psycopg[binary]>=3.2  +  python-dotenv>=1.0
├── supabase/
│   ├── config.toml
│   ├── migrations/          ← .sql files (schema + functions as code)
│   └── functions/
│       └── property-search/
│           └── index.ts     ← the API endpoint
└── docs/
    ├── RUNBOOK.md
    └── GUIDE.md             ← this file

data/        ← the downloaded .txt files  (OUTSIDE the repo, never committed)
reference/   ← PDFs / specs               (OUTSIDE the repo)
```

Initialize git:
```powershell
cd d:\Warren\twg-api
git init
```

**`.gitignore`** must include (so secrets/data never get committed):
```
.env
__pycache__/
.venv/
*.txt
*.csv
*.zip
data/
```

---

## 3. Step 1 — Download the data (SFTP)

The files live on an FTP server. Ours was **SFTP on port 22**.

1. Open **WinSCP** → New Session:
   - **File protocol:** SFTP
   - **Host name:** `dataftp.thewarrengroup.com`
   - **Port:** `22`
   - **User name / Password:** (from the provider)
2. Connect, navigate to the file-storage folder, and **download** the state files
   (e.g. `Weekly_DC_Prop07012026.txt`) into your local `data/` folder.
3. If they arrive zipped, **unzip** them.

> **Gotcha — "SFTP won't connect":** make sure the protocol is **SFTP** and port **22**
> (not plain FTP on 21). A US IP / stable connection helped in our case.

---

## 4. Step 2 — Understand the data (before loading)

Never load blind — look first. Our files were:

- **Tab-delimited** (TSV), **229 columns**, one **header row**, plain ASCII,
  **CRLF** (Windows) line endings.
- **First two columns:** `FIPS` + `PropertyID` → together they're the unique key.
- **Big:** one small state ≈ 200 MB, the largest (Arkansas) ≈ **1.8 GB / 2.3M rows**.
  Total for 10 states ≈ 5 GB / ~7M rows.

Why this matters:
- Tab-delimited + CRLF → the loader must use the right `COPY` settings (see §6).
- Every column is loaded as **TEXT** (numbers/dates are text) — this affects filtering later (§9).
- The size means we load **smallest-first** and watch disk (§6, §10).

To peek at a file's header quickly (Git Bash):
```bash
head -n 1 data/Weekly_DC_Prop07012026.txt | tr '\t' '\n' | nl
```

---

## 5. Step 3 — Get your Supabase connection

1. Sign in to Supabase → open your project (ours: **Contractor Project**).
2. Click **Connect** (top of the dashboard).
3. Choose **Session pooler** — **not** "Direct connection".
   - **Why:** Direct connection is **IPv6-only** and times out from most home/VPN
     (IPv4) networks. The **Session pooler (port 5432)** is IPv4-friendly and is the
     right choice for a bulk loader.
4. Copy the connection string. It looks like:
   ```
   postgresql://postgres.<PROJECT_REF>:<DB_PASSWORD>@aws-1-<region>.pooler.supabase.com:5432/postgres
   ```
5. **Database password:** it's **not viewable after project creation.** Either get it
   from whoever created the project, or **Database → Settings → Reset password** (if you
   have permission — a restricted role can't; ask an admin).

> **Gotcha — "You need additional permissions to reset the database password":** your
> project role is restricted. Ask the project owner to send the password or reset it.

---

## 6. Step 4 — The Python loader

### 6a. Create a virtual environment (venv)

A **venv** is an isolated Python environment per project (so packages don't clash).

```powershell
cd d:\Warren\twg-api
python -m venv .venv
```

Activate it:
```powershell
.venv\Scripts\Activate.ps1
```

> **Gotcha — "running scripts is disabled on this system":** run this once, then retry:
> ```powershell
> Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
> ```
> **Gotcha — wrong venv (`ModuleNotFoundError: No module named 'psycopg'`):** you
> activated a different venv. Always use the project one at `twg-api\.venv`, or call it
> by full path: `d:\Warren\twg-api\.venv\Scripts\python.exe ...`

### 6b. Install dependencies

`loader/requirements.txt`:
```
psycopg[binary]>=3.2
python-dotenv>=1.0
```
Install:
```powershell
pip install -r loader\requirements.txt
```
- **psycopg** = the Postgres driver (the `[binary]` gets a prebuilt version, no compiler needed).
- **python-dotenv** = lets the loader read your `.env` file automatically.

### 6c. Configure `.env`

`twg-api\.env` (gitignored):
```
SUPABASE_DSN=postgresql://postgres.<PROJECT_REF>:<DB_PASSWORD>@aws-1-<region>.pooler.supabase.com:5432/postgres?sslmode=require
TWG_SOURCE=local
TWG_LOCAL_DIR=d:\Warren\data
```
- Use the **Session pooler** string (port 5432), add `?sslmode=require`.
- URL-encode any special characters in the password.

### 6d. What the loader does (the key ideas)

The loader (`loader/loader.py`) for each file:
1. Reads the header row → builds a **staging table** with all 229 columns as `TEXT`
   (nothing hard-coded).
2. **Streams** the file into staging with `COPY` (block by block — an 8 MB buffer, so
   even the 1.8 GB file never loads into memory).
3. **Upserts** staging → the one `public.properties` table on `(fips, propertyid)`, so
   re-loading the same file **won't create duplicates** (idempotent).
4. Splits the work into separate transactions (COPY / upsert / VACUUM) to protect disk.

The `COPY` settings that matter for this data:
```
FORMAT csv, DELIMITER E'\t', HEADER true, NULL '',
QUOTE E'\x01', ESCAPE E'\x01', ENCODING 'LATIN1'
```
- `DELIMITER E'\t'` — tabs.  `HEADER true` — skip the header row.  `NULL ''` — empty → NULL.
- `QUOTE/ESCAPE E'\x01'` — a byte that never appears in the data, so literal `"` and `\`
  in the text (owner names, legal descriptions) are treated as data, not special chars.
- `ENCODING 'LATIN1'` — accepts every byte, so one stray accented character can't abort
  a 7-million-row load.

### 6e. Run the loader

```powershell
# List the files it will load (no database needed):
python loader\loader.py --list

# Load the SMALLEST state first to prove the pipeline:
python loader\loader.py --only DC

# Load the rest, smallest-first:
python loader\loader.py --only VT WY RI DE SD NH HI ND
```

> **Gotcha — disk full loading the biggest file:** on the Supabase **SMALL tier (8 GB
> disk)**, loading Arkansas (1.8 GB) ran out of space during the sort/upsert and rolled
> back (`DiskFull: No space left on device`). The smaller states stayed fine. **For a
> POC, skip the biggest file** — or raise the disk (Database → Settings → Disk size) and
> then load it. Clean up a failed load's leftover with:
> ```sql
> drop table if exists public.properties_staging;
> ```

---

## 7. Step 5 — Verify & index the data (SQL Editor)

Open Supabase → **SQL Editor**.

**Verify the load:**
```sql
select count(*) from public.properties;
select situsstate, count(*) from public.properties group by situsstate order by 2 desc;
```
> **Note — column names are lowercase.** The loader lowercases all 229 columns, so use
> `situsstate`, `situszip5`, `fips` (not `SitusState`). Quoted mixed-case like
> `"SitusState"` will error with "column does not exist".

**Grant read access to the API roles** (the loader created the table as `postgres`, so
the API roles need explicit permission — otherwise the endpoint returns
`permission denied for table properties`):
```sql
grant usage on schema public to service_role, anon, authenticated;
grant select on public.properties to service_role, anon, authenticated;
alter default privileges in schema public grant select on tables to service_role, anon, authenticated;
```

**Measure speed with `EXPLAIN ANALYZE`:**
```sql
explain analyze
select fips, propertyid, situscity from public.properties
where situsstate = 'DC' and situszip5 = '20037'
order by propertyid limit 50;
```
Look for **"Index Scan"** (good) vs **"Seq Scan"** (bad — scans everything), and the
**Execution Time**.

**Add indexes** for the columns you filter/sort on. We hit a ~10-second state-only query
because the sort wasn't index-supported, and fixed it with:
```sql
create index if not exists properties_state_pid_idx on public.properties (situsstate, propertyid);
create index if not exists properties_fips_pid_idx  on public.properties (fips, propertyid);
create index if not exists properties_apn_idx        on public.properties (apn);
analyze public.properties;
```
That took the state-only query from **~10,000 ms → ~20 ms**. Indexes are the single
biggest speed win.

---

## 8. Step 6 — Build & deploy the API endpoint

The endpoint is a **Supabase Edge Function** (TypeScript/Deno) at
`supabase/functions/property-search/index.ts`. It reads query params, calls the database,
and returns JSON.

### 8a. Deploy with the Supabase CLI

```powershell
cd d:\Warren\twg-api
supabase login                                        # opens a browser to authorize
supabase link --project-ref <PROJECT_REF>             # may ask for the DB password
supabase functions deploy property-search --project-ref <PROJECT_REF>
```

> **Gotcha — `supabase: command not found` right after installing it:** your terminal's
> PATH is stale. Open a **new** terminal, or reload PATH in the current one:
> ```powershell
> $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
> ```
> In **VS Code**, fully **restart the app** so its integrated terminals pick up the new PATH.
>
> **Gotcha — "Docker is not running" warning on deploy:** harmless. Docker is only needed
> for *local* function testing, not for deploying to the cloud.

### 8b. Test it

```powershell
curl.exe -s "https://<PROJECT_REF>.supabase.co/functions/v1/property-search?state=DC&zip=20037&pagesize=5"
```
Expect JSON: `{ "rows": [...], "page": 1, "pagesize": 5, ... }`.

> **Gotcha — 401 Unauthorized:** the function requires a JWT by default. Redeploy without
> it for the POC:
> ```powershell
> supabase functions deploy property-search --no-verify-jwt
> ```
> (Or send your project's anon key: `-H "Authorization: Bearer <ANON_KEY>"`.)
>
> **Gotcha — `permission denied for table properties`:** run the `grant select` from §7.

---

## 9. Step 7 — Flexible field filtering (the search function)

To let customers filter on many fields, we put the logic in a Postgres **function (RPC)**
and had the endpoint call it. Our fields: `fips`, `apn`, `situsstate`, `situszip5`
(equality) and `yearbuilt`, `taxamt`, `assdtotalvalue`, `prevsalerecordingdate`,
`prevsalecontractdate` (ranges).

Two important lessons shaped the final design:

**Lesson 1 — don't compute an exact total on broad queries.** Our first version used
`count(*) over()` to return a total. That forces Postgres to scan *every* matching row,
which blew past the API's **8-second statement timeout**. Fix: drop the exact count and
return a **`hasMore`** flag instead — then `LIMIT` lets the query stop early (fast).

**Lesson 2 — build the SQL dynamically, not with `(param IS NULL OR col = param)`.** A
static function body with that pattern works when you call it with *constant* values
(SQL Editor) but, when called through the API with **bind parameters**, forces a
"generic plan" that **can't use the index** → full scan → timeout again. The fix is a
**plpgsql function that builds the WHERE clause from only the filters you actually
passed**, so every call gets an index-using plan. Values are escaped with
`quote_literal` (text) or cast `::text` (numbers) → injection-safe.

The final function lives in
`supabase/migrations/20260701120000_search_properties.sql`. Run its full contents in the
SQL Editor (it drops and recreates the function). The endpoint
(`property-search/index.ts`) maps query params → function params and returns
`{ rows, page, pagesize, hasMore, query_ms }`.

Because numeric/date columns are stored as **text**, the function casts them safely with a
regex guard, e.g.:
```sql
p.taxamt ~ '^[0-9]+([.][0-9]+)?$' and p.taxamt::numeric >= <min>
```
Dates are `YYYYMMDD` strings, which sort correctly as text, so date ranges work directly.

**Deploy note:** changing only the *database function* needs **no endpoint redeploy** —
the endpoint calls it the same way. Just run the SQL and re-test the URL.

Example requests (base `https://<PROJECT_REF>.supabase.co/functions/v1/property-search`):
```
?state=DC
?fips=11001
?apn=37-00-00708-00
?state=DC&yearBuiltMin=1950&yearBuiltMax=2000
?state=DC&taxAmtMin=5000&taxAmtMax=20000
?state=DC&assdValueMin=1000000
?state=DC&recDateFrom=20060101&recDateTo=20061231
?state=DC&contractDateFrom=20060101&contractDateTo=20061231
```
> **Tip:** always pair a range filter with an indexed field (`state`/`fips`/`apn`) to keep
> it fast; a range-only query has to scan.

---

## 10. Troubleshooting — every error we hit

| Symptom | Cause | Fix |
|---|---|---|
| FileZilla installer flagged as malware | Bundled adware (PUA) | Use **WinSCP**, or Decline every offer in FileZilla's installer |
| SFTP won't connect | Wrong protocol/port | SFTP on **port 22**; use a stable/US IP |
| DB connection times out | Using **Direct connection** (IPv6) | Use the **Session pooler** (port 5432) |
| Can't reset DB password | Restricted project role | Ask the project owner for it |
| `running scripts is disabled` | PowerShell policy | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| `ModuleNotFoundError: psycopg` | Wrong/empty venv active | Use `twg-api\.venv` (or its full `python.exe` path) |
| `DiskFull: No space left on device` | 8 GB disk too small for the 1.8 GB file | Skip the big file for POC, or raise the disk; drop `properties_staging` |
| `column "SitusState" does not exist` | Columns are **lowercase** | Use `situsstate`, `situszip5`, etc. |
| `supabase: command not found` (just installed) | Stale PATH | New terminal / reload PATH / restart VS Code |
| `Docker is not running` on deploy | — | Harmless; only needed for local testing |
| Endpoint returns **401** | JWT required by default | `supabase functions deploy property-search --no-verify-jwt` |
| `permission denied for table properties` | API roles lack SELECT grant | `grant select on public.properties to service_role, anon, authenticated;` |
| Endpoint **8s timeout** (had `count(*) over()`) | Exact count scans all rows | Drop the count; return `hasMore` |
| Endpoint **8s timeout** (parameterized call) | Generic plan can't use index | Build SQL **dynamically** in a plpgsql function |

---

## 11. Command cheat-sheet

```powershell
# --- one-time setup ---
python -m venv .venv
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned      # if activation is blocked
.venv\Scripts\Activate.ps1
pip install -r loader\requirements.txt
irm get.scoop.sh | iex ; scoop install supabase          # Supabase CLI

# --- load data ---
python loader\loader.py --list
python loader\loader.py --only DC
python loader\loader.py --only VT WY RI DE SD NH HI ND

# --- deploy endpoint ---
cd d:\Warren\twg-api
supabase login
supabase link --project-ref <PROJECT_REF>
supabase functions deploy property-search --project-ref <PROJECT_REF>

# --- test ---
curl.exe -s "https://<PROJECT_REF>.supabase.co/functions/v1/property-search?state=DC&pagesize=5"
```

Key SQL (run in the SQL Editor):
```sql
-- grants (once)
grant select on public.properties to service_role, anon, authenticated;
-- indexes (once)
create index if not exists properties_state_pid_idx on public.properties (situsstate, propertyid);
-- the search function
-- (run the full file: supabase/migrations/20260701120000_search_properties.sql)
```

---

## 12. Glossary (new-to-this terms)

- **Supabase** — hosted PostgreSQL database + tools (auth, auto REST API, Edge Functions).
- **Postgres / PostgreSQL** — the relational database underneath.
- **Session pooler** — a connection endpoint (port 5432) that works over IPv4 and suits
  long bulk loads. (Transaction pooler = 6543, for short serverless calls.)
- **venv** — an isolated Python environment per project.
- **psycopg** — the Python library for talking to Postgres.
- **COPY** — Postgres' fast bulk-load command.
- **Upsert** — insert, or update if the key already exists (`INSERT ... ON CONFLICT`).
- **Idempotent** — running it again gives the same result (no duplicates).
- **Index** — a data structure that makes filtered/sorted queries fast.
- **RPC** — a database function you can call over the API (here, `search_properties`).
- **Edge Function** — Supabase's serverless TypeScript function; our API endpoint.
- **Supabase CLI** — the command-line tool to deploy functions and manage the project.
- **RLS (Row Level Security)** — per-row DB access rules (off in this POC; a production
  hardening step).
- **statement_timeout** — a limit (8s on the API) that cancels slow queries.

---

*You now have the full path from empty folder to a live, filterable property API. Keep
this file in `docs/` — next time, follow §1 → §9 in order, and use §10 when something
breaks.*
