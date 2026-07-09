# TWG API — Property Data Platform

Data Access API for The Warren Group property data, built on Supabase.

**Current phase — proof of concept:** load the per-state property files into one
Postgres table and expose a `state` + `zip` search endpoint, then measure speed.

## Structure

```
twg-api/
├── loader/                     # Python data loader (TSV -> Supabase Postgres)
│   ├── loader.py               # streaming, idempotent loader (swappable source)
│   └── requirements.txt
├── supabase/                   # Supabase project (CLI-managed)
│   ├── config.toml
│   ├── migrations/             # schema as code (capture via `supabase db pull`)
│   └── functions/
│       └── property-search/
│           └── index.ts        # the state/zip POC endpoint
├── docs/
│   └── RUNBOOK.md              # step-by-step load + deploy + test guide
├── .env.example                # copy to .env and fill in (gitignored)
└── .gitignore
```

## Quickstart

1. `copy .env.example .env` and fill in `SUPABASE_DSN` (use the **Session pooler**, port **5432**).
2. `pip install -r loader/requirements.txt`
3. Follow **docs/RUNBOOK.md** — load DC first, verify, then the rest.

## Key facts (read before touching anything)

- **Data:** 10 per-state TSV files, **229 columns**, ~7M rows total. The raw files live
  **outside the repo** (see `TWG_LOCAL_DIR` in `.env`) — never commit them.
- **All DB column names are lowercase**: `fips`, `propertyid`, `situsstate`, `situszip5`,
  `situsfullstreetaddress`, `situslatitude`, `situslongitude`, ... Use lowercase in all SQL and endpoints.
- **Unified table:** `public.properties`, composite PK `(fips, propertyid)`,
  index `(situsstate, situszip5)`. All states load into this one table.
- **Loader connection:** Supabase **Session pooler (5432)** only — never the transaction pooler (6543).
- **Out of scope for the POC:** RLS, response envelope, GraphQL, rate limiting, PostGIS.
  (Lat/long columns exist, so PostGIS radius search is a future option.)

## First commit

```
git add .
git commit -m "Scaffold TWG API POC: loader, endpoint, runbook"
```
