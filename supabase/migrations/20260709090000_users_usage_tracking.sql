-- Users + API keys + usage logging, per the TWG spec tabs Sam shared:
--   UserProfile / UserAPIKeys / UsageLogs / ErrorReturnPackage.
--
-- Notes vs the spec:
--   * Table/column names are snake_case (Postgres convention); JSON output can map to
--     the spec's UpperCamelCase later at the API layer.
--   * UserProfile.Password/Secret are OMITTED for now — portal login will use Supabase
--     Auth later; keys here are plaintext for the POC (hash before production).
--   * Spec's UserAPIKeys has no status column (validity = expiration date); a status
--     column is added anyway so a key can be revoked immediately without date games.
--   * "Key cycle" = usage since key_create_date. Rotation must create a NEW key row
--     (new cycle) — never mutate key_create_date on an existing row.
--   * "Today" for the daily limit = UTC calendar day (made explicit below). The spec
--     leaves the COB timezone TBD — confirm with Mike.
--   * Only SUCCESSFUL (code 0) responses count toward record quotas, per API
--     Requirements ("count only successful 200 responses toward quota").
--   * Quota enforcement is a pre-request check against usage recorded so far; a burst
--     of parallel requests can overshoot by up to a page each (documented POC behavior;
--     production gets atomic counters, e.g. Redis, per the feasibility assessment).
--   * usage_logs.http_status is an ADDITION to the spec (needed to audit HTTP behavior;
--     TWGStatusCode per spec is TWG's own 1000+ code, not the HTTP status).
--
-- NOTE: the old public.api_keys table is NOT dropped here — the currently deployed
-- function still reads it. Drop it with the follow-up migration
-- 20260709120000_drop_old_api_keys.sql AFTER the new function version is deployed
-- and verified.

-- ---------------------------------------------------------------------------
-- 1) user_profile  (spec: UserProfile)
-- ---------------------------------------------------------------------------
create table if not exists public.user_profile (
  user_id                   bigint generated always as identity primary key,
  status                    char(1)     not null default 'A' check (status in ('A','I')),
  user_name                 text        unique,
  user_first_name           text,
  user_last_name            text,
  user_company              text,
  reference_id              text,                          -- TWG internal customer ref
  account_duration          text        not null default '1MN',   -- 1DAY/7DAYS/1MN/3MN/12MN
  auto_renew                char(1)     not null default 'Y' check (auto_renew in ('Y','N')),
  max_records_per_query     int         not null default 100,
  max_records_per_day       int         not null default 100000,
  max_records_per_key_cycle int         not null default 1000000,
  create_date               timestamptz not null default now(),
  last_mod_date             timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- 2) user_api_keys  (spec: UserAPIKeys; 1:many from user_profile)
-- ---------------------------------------------------------------------------
create table if not exists public.user_api_keys (
  id                        bigint generated always as identity primary key,
  user_id                   bigint      not null references public.user_profile(user_id),
  twg_api_key               text        not null unique,   -- PLAINTEXT for POC; hash for prod
  status                    char(1)     not null default 'A' check (status in ('A','I')),
  -- per-key overrides; when NULL the user_profile limit applies
  max_records_per_query     int,
  max_records_per_day       int,
  max_records_per_key_cycle int,
  key_create_date           timestamptz not null default now(),
  key_expiration_date       timestamptz not null
);
-- (no extra index on twg_api_key: the UNIQUE constraint already provides one)

-- ---------------------------------------------------------------------------
-- 3) usage_logs  (spec: UsageLogs; 1:many from user_api_keys)
--    Logs the request params + the returned STATUS portion. Never the data payload.
-- ---------------------------------------------------------------------------
create table if not exists public.usage_logs (
  uid                     bigint generated always as identity primary key,
  twg_transaction_id      uuid        not null,
  user_id                 bigint,                -- null when the key was unknown
  twg_api_key             text,                  -- as sent by the caller
  twg_status_code         text,                  -- TWG code (1000+ range; 1000 = OK)
  http_status             int,                   -- addition to spec: the HTTP status sent
  request_query_parm_json jsonb,                 -- full query params, stored as-is
  returned_version        text,
  returned_code           int,
  returned_msg            text,
  total_matched           int,
  page_number             int,
  num_records_returned    int,
  returned_date_time      timestamptz,
  requested_date_time     timestamptz not null default now()
);

-- serves the daily/cycle quota sums and future billing rollups
create index if not exists usage_logs_key_time_idx
  on public.usage_logs (twg_api_key, requested_date_time);

-- ---------------------------------------------------------------------------
-- 4) validate_api_key(p_key) -> one round-trip: key + coalesced limits + usage so far
-- ---------------------------------------------------------------------------
create or replace function public.validate_api_key(p_key text)
returns jsonb
language sql
stable
as $$
  with k as (
    select k.user_id,
           k.twg_api_key,
           coalesce(k.max_records_per_query,     u.max_records_per_query)     as max_q,
           coalesce(k.max_records_per_day,       u.max_records_per_day)       as max_d,
           coalesce(k.max_records_per_key_cycle, u.max_records_per_key_cycle) as max_c,
           k.key_create_date,
           (k.status = 'A' and u.status = 'A' and now() < k.key_expiration_date) as valid
    from public.user_api_keys k
    join public.user_profile u on u.user_id = k.user_id
    where k.twg_api_key = p_key
    limit 1
  ),
  usage as (
    select
      coalesce(sum(l.num_records_returned) filter (
        where l.requested_date_time >=
              (date_trunc('day', now() at time zone 'utc') at time zone 'utc')), 0) as used_today,
      coalesce(sum(l.num_records_returned), 0)                                      as used_cycle
    from public.usage_logs l
    join k on l.twg_api_key = k.twg_api_key
    where l.requested_date_time >= k.key_create_date   -- current key cycle only
      and l.returned_code = 0                          -- only successes count
  )
  select case
    when not exists (select 1 from k) then jsonb_build_object('found', false)
    else (
      select jsonb_build_object(
        'found', true,
        'valid', k.valid,
        'user_id', k.user_id,
        'max_q', k.max_q, 'max_d', k.max_d, 'max_c', k.max_c,
        'used_today', usage.used_today, 'used_cycle', usage.used_cycle)
      from k, usage)
  end;
$$;

-- ---------------------------------------------------------------------------
-- 5) Security: the edge function (service_role) is the ONLY door.
--    RLS on + explicit grants + REVOKES. The revokes matter: Postgres grants
--    function EXECUTE to PUBLIC by default, and Supabase default privileges give
--    anon/authenticated access to new objects in public — without the revokes,
--    anyone holding the project's public anon key could call search_properties
--    straight through PostgREST (/rest/v1/rpc/...) with no API key, no quotas,
--    and no usage logging.
-- ---------------------------------------------------------------------------
alter table public.user_profile  enable row level security;
alter table public.user_api_keys enable row level security;
alter table public.usage_logs    enable row level security;
alter table public.properties    enable row level security;   -- idempotent; closes the data table too

revoke all on public.user_profile  from public, anon, authenticated;
revoke all on public.user_api_keys from public, anon, authenticated;
revoke all on public.usage_logs    from public, anon, authenticated;
revoke all on public.properties    from public, anon, authenticated;

revoke execute on function public.validate_api_key(text) from public, anon, authenticated;
revoke execute on function public.search_properties(
  text, text, text, text, int, int, numeric, numeric, numeric, numeric,
  text, text, text, text, text[], boolean, int, int
) from public, anon, authenticated;

grant select                        on public.user_profile  to service_role;
grant select                        on public.user_api_keys to service_role;
grant select, insert                on public.usage_logs    to service_role;
grant select                        on public.properties    to service_role;
grant execute on function public.validate_api_key(text)     to service_role;
grant execute on function public.search_properties(
  text, text, text, text, int, int, numeric, numeric, numeric, numeric,
  text, text, text, text, text[], boolean, int, int
) to service_role;

-- ---------------------------------------------------------------------------
-- 6) Seed the POC demo user + key (matches the key in the old api_keys table)
-- ---------------------------------------------------------------------------
do $$
declare
  v_user bigint;
begin
  if not exists (select 1 from public.user_api_keys where twg_api_key = 'twg_demo_2026') then
    insert into public.user_profile (user_name, user_company)
    values ('poc_demo', 'POC test customer')
    returning user_id into v_user;

    insert into public.user_api_keys (user_id, twg_api_key, key_expiration_date)
    values (v_user, 'twg_demo_2026', now() + interval '90 days');
  end if;
end $$;
