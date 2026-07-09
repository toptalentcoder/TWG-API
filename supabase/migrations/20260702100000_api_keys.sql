-- API key table for endpoint authorization (Nick request #1).
-- POC: keys are stored in plaintext for simplicity. For production, store a HASH
-- (e.g. via pgcrypto's crypt()) and compare hashes instead.

create table if not exists public.api_keys (
  id         bigint generated always as identity primary key,
  key        text        not null unique,
  customer   text,
  active     boolean     not null default true,
  created_at timestamptz not null default now()
);

-- Keys are sensitive: only the service role (used by the Edge Function) may read them.
-- RLS on + no policy means anon/authenticated get nothing; service_role bypasses RLS.
alter table public.api_keys enable row level security;
grant select on public.api_keys to service_role;

-- Seed one test key for the POC. Change/rotate these later; add one row per customer.
insert into public.api_keys (key, customer)
values ('twg_demo_2026', 'POC test customer')
on conflict (key) do nothing;
