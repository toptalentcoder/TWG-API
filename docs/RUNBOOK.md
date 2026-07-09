# TWG Property Data POC — Runbook

Load 10 weekly per-state property files (~5 GB / ~7M rows) from The Warren Group
into ONE unified Supabase Postgres table, then expose a state+zip search endpoint.

This runbook is PowerShell-native (Windows). It needs only Python + psycopg3 and
the Supabase dashboard/CLI — no `psql` required.

---

## Ground truth (verified against the real files)

- **229 columns**, TAB-delimited, header row present, **CRLF** line endings, plain
  ASCII. Last column is **`VacantFlagDate`**.
- **DC = 215,430 data rows**; 10 files total ~5 GB / ~6.98M rows.
- Smallest-first byte order (validate on the smallest, load the biggest last):
  `DC (178 MB) < VT (237 MB) < WY (276 MB) < RI (313 MB) < DE (405 MB) < SD (490 MB) < NH (502 MB) < HI (503 MB) < ND (506 MB) < AR (1,799 MB)`.
- DC has 0 backslashes / 2 stray `"`; the big **AR** file carries ~16k literal `"`
  and 709 literal `\` as ordinary field data (owner names, legal descriptions).
  There are **zero `\x01` bytes** anywhere in the data.

Why the loader uses
`FORMAT csv, DELIMITER E'\t', HEADER true, NULL '', QUOTE E'\x01', ESCAPE E'\x01', ENCODING 'LATIN1'`:
the control byte `0x01` never occurs in this data, so `"` is treated as literal;
in CSV mode a backslash is not special, so AR's `\` are already literal; CSV mode
strips the CRLF terminator; and `ENCODING 'LATIN1'` accepts every byte so a stray
accented owner name cannot abort a multi-million-row COPY. `FORMAT text` would
corrupt AR — deliberately avoided.

---

## Supporting files

- **`d:\Warren\twg-api\loader\loader.py`** — streaming, idempotent psycopg3 loader
  (smallest-first order, AR last; `--list` runs without a DB).
- **`d:\Warren\twg-api\supabase\functions\property-search\index.ts`** — the state+zip POC endpoint
  (Supabase Edge Function; deploy under `supabase/functions/property-search/`).
- **`create_properties.sql`** (optional) — the loader auto-creates the table, but
  if you want to run DDL by hand it is 229 `text` columns plus
  `constraint properties_pkey primary key (fips,propertyid)`.

---

## The 10 steps

### 1. Get the SESSION pooler connection string (port 5432, NOT 6543)

Supabase dashboard → Project → **Connect** → **Session pooler**. It ends in
`...pooler.supabase.com:5432/postgres`. Set it (URL-encode any special chars in
the password):

```powershell
$env:SUPABASE_DSN = "postgresql://postgres.<ref>:<pw>@aws-1-<region>.pooler.supabase.com:5432/postgres?sslmode=require"
```

> The **session** pooler (5432) is required. On the **transaction** pooler (6543)
> the loader's session `SET statement_timeout=3600s` silently resets per
> transaction and the 1.8 GB AR COPY would abort. `loader.py` now **probes** the
> effective `statement_timeout` right after connecting and **fails fast** with a
> clear message if you used 6543 — so a wrong string is caught in seconds, not
> mid-AR.

### 2. Install psycopg3

```powershell
pip install "psycopg[binary]>=3.2"
```

### 3. (Optional) Pre-create the table

The loader creates `public.properties` (all TEXT), the composite PK
`(FIPS, PropertyID)`, and the `(SitusState, SitusZIP5)` index automatically. Skip
this step unless you want to run `create_properties.sql` by hand first. Do **not**
add extra indexes yet — they only slow the initial load.

### 4. Load DC first, then verify + prove idempotency

```powershell
python d:\Warren\twg-api\loader\loader.py --only DC
```

Verify in the Supabase SQL editor:

```sql
select count(*) from public.properties;             -- expect 215430
select fips,propertyid,situsfullstreetaddress,situscity,situsstate,situszip5
from public.properties where situsstate = 'DC' limit 5;
```

Re-run the exact same load and re-check the count — it must **stay 215,430**
(idempotent upsert on the composite key):

```powershell
python d:\Warren\twg-api\loader\loader.py --only DC
```

### 5. Check disk headroom before the big files

```sql
select pg_size_pretty(pg_database_size(current_database())) as db_size;
select pg_size_pretty(pg_total_relation_size('public.properties')) as table_total;
```

The full 10-file DB lands around ~8.5 GB, which is over the 8 GB default disk.
Supabase disk autoscales, but it lags a fast bulk load — bump it manually first
(next step) so AR doesn't hit disk-full. The loader also splits each file into
COPY / upsert / `VACUUM (ANALYZE)` as **separate transactions**, so AR's WAL and
re-load dead tuples are retired between phases instead of piling up in one giant
transaction.

### 6. Raise the disk, then load all files smallest-first (AR last)

Supabase dashboard → Database → **Disk size** → set to **20 GB**, wait for it to
apply, then:

```powershell
python d:\Warren\twg-api\loader\loader.py
```

This loads all 10 files smallest-first (DC → … → AR). Per-file log lines show
`copied=`, `upserted=`, and copy/upsert timing. Total is roughly ~7M rows.

### 7. Confirm the filter index + refresh stats

The loader creates the `(SitusState, SitusZIP5)` index and runs
`VACUUM (ANALYZE)` after each file. Confirm and (re)analyze if desired:

```sql
select indexname from pg_indexes where tablename = 'properties';
analyze public.properties;
```

### 8. Deploy and curl-test the endpoint

Place `index.ts` at `supabase/functions/property-search/index.ts`, add a
`config.toml` with `verify_jwt = false` for the POC, then:

```powershell
supabase functions deploy property-search --project-ref <PROJECT_REF>
curl.exe -s "https://<PROJECT_REF>.functions.supabase.co/property-search?state=DC&zip=20037&page=1&pagesize=50"
```

Expect JSON `{ rows, total, page, pagesize, query_ms }`.

### 9. Confirm index usage + measure latency

```sql
explain analyze
select fips,propertyid from public.properties
where situsstate = 'DC' and situszip5 = '20037'
order by propertyid limit 50;
```

Confirm an **Index Scan** (not Seq Scan — a Seq Scan means you skipped `ANALYZE`
in step 7). Then a 20-call latency loop for a rough p50/p95:

```powershell
$u = "https://<PROJECT_REF>.functions.supabase.co/property-search?state=DC&zip=20037&pagesize=50"
$times = 1..20 | ForEach-Object { (Measure-Command { curl.exe -s $u | Out-Null }).TotalMilliseconds }
$sorted = $times | Sort-Object
"p50 = {0:N0} ms  p95 = {1:N0} ms" -f $sorted[9], $sorted[18]
```

> If latency is dominated by the count, the endpoint uses `count: "exact"` which
> runs a real `COUNT(*)`. Switch to `{ count: "estimated" }` in `index.ts` (planner
> stats, near-instant) if the speed test feels slow on the SMALL tier.

### 10. Report to Nick (template)

```
POC status: 10 states (~7M rows) loaded into ONE Supabase table `public.properties`.
Endpoint: GET /property-search?state=XX&zip=NNNNN&page=&pagesize= -> {rows,total,page,pagesize,query_ms}.
DC verified at 215,430 rows; re-load is idempotent (count unchanged).
Query path: Index Scan on (SitusState, SitusZIP5); p50/p95 = <fill in> ms.
Heads-up for all 50 states: ~5x the data -> larger disk + likely a MEDIUM compute tier;
  spatial (lat/long radius) search would want PostGIS + a GiST index, out of scope for this POC.
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Loader raises "statement_timeout did not persist … TRANSACTION pooler (port 6543)" | Used the 6543 transaction pooler string | Reconnect with the **session** pooler (5432) or a direct connection |
| AR COPY aborts partway with a timeout | Timeout `SET` didn't stick (wrong pooler) | Same as above — use 5432 |
| `invalid byte sequence for encoding "UTF8"` | Stray accented byte, COPY without `ENCODING 'LATIN1'` | Already handled — loader sets `ENCODING 'LATIN1'` |
| Disk-full during AR | 8 GB default disk too small for ~8.5 GB DB | Raise disk to 20 GB (step 6) before loading AR |
| Endpoint returns Seq Scan / slow | `ANALYZE` skipped or index missing | Run `analyze public.properties` (step 7); confirm the index exists |
| Password with special chars breaks the DSN | Unencoded `@`, `:`, `/`, etc. in the password | URL-encode the password in `SUPABASE_DSN` |
| Loader errors "cannot affect row a second time" | Intra-file duplicate key in a future drop | Already guarded — upsert uses `DISTINCT ON (FIPS, PropertyID)` |

---

## Design notes

- **Streaming**: 8 MiB block COPY — peak client memory is ~8 MiB regardless of the
  1.8 GB AR file size.
- **Idempotency**: unlogged staging →
  `INSERT ... SELECT DISTINCT ON (FIPS, PropertyID) ... ON CONFLICT DO UPDATE`.
  Overwrites all non-key columns for present keys. Note: no row-deletion semantics
  (a shrinking drop leaves stale rows), and the `DISTINCT ON` tiebreak is arbitrary
  for a hypothetical intra-file dup (there is no recency column in the data).
- **Transaction shape**: COPY, upsert, and `VACUUM (ANALYZE)` are separate
  committed transactions per file to bound WAL/bloat on the SMALL tier.
- **Swappable source**: `LocalSource` today; `SFTPSource` / `GCSSource` seams are in
  `loader.py` — only three methods change, the COPY path is identical.
- **Endpoint**: `state` is required so every query is served by the
  `(SitusState, SitusZIP5)` index; `.eq()` parameterizes inputs (injection-safe);
  `pagesize` is clamped to `[1, 100]`.
