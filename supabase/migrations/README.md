# Migrations

For the POC, the `public.properties` table (229 `text` columns, composite PK on
`(fips, propertyid)`, index on `(situsstate, situszip5)`) is **created automatically**
by `loader/loader.py` on first run — so you don't need a migration to start.

To capture the real schema as a version-controlled migration once data is loaded:

```
supabase link --project-ref <PROJECT_REF>
supabase db pull
```

This writes the canonical `CREATE TABLE` here. We use `db pull` rather than
hand-writing 229 columns so the migration exactly matches the loader's normalized,
lowercased column names.
