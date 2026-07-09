-- Flexible property search over public.properties.
--
-- Returns SETOF jsonb (one JSON object per row) so the caller can choose which columns
-- come back:
--   * default: a lean, useful subset
--   * p_fields: add any extra real columns (validated against the table -> injection-safe)
--   * p_all_fields = true: the entire row (all 229 columns)
--
-- Built with dynamic SQL so it (a) always uses an index (a static "(param is null or
-- col = param)" body forces a generic plan that can't use the index under the API's bind
-- parameters -> full scan -> 8s timeout) and (b) supports dynamic column selection.
-- Values are escaped with quote_literal / cast ::text; column names are validated against
-- information_schema and quoted with %I -> injection-safe. No exact total (LIMIT short-circuits).

drop function if exists public.search_properties(
  text, text, text, text, int, int, numeric, numeric, numeric, numeric,
  text, text, text, text, int, int
);

create or replace function public.search_properties(
  p_fips              text     default null,
  p_apn               text     default null,
  p_state             text     default null,
  p_zip               text     default null,
  p_yearbuilt_min     int      default null,
  p_yearbuilt_max     int      default null,
  p_taxamt_min        numeric  default null,
  p_taxamt_max        numeric  default null,
  p_assdvalue_min     numeric  default null,
  p_assdvalue_max     numeric  default null,
  p_recdate_from      text     default null,
  p_recdate_to        text     default null,
  p_contractdate_from text     default null,
  p_contractdate_to   text     default null,
  p_fields            text[]   default null,
  p_all_fields        boolean  default false,
  p_limit             int      default 10,
  p_offset            int      default 0
)
returns setof jsonb
language plpgsql
stable
as $func$
declare
  conds text := 'true';
  nlim  int  := greatest(1, least(coalesce(p_limit, 10), 101));  -- 101 = 100 max page + the endpoint's +1 "next page" probe
  noff  int  := greatest(0, coalesce(p_offset, 0));
  sel   text;
  q     text;
  valid_cols text[];
  want_cols  text[];
  default_cols constant text[] := array[
    'fips','propertyid','apn','situsfullstreetaddress','situscity','situsstate',
    'situszip5','yearbuilt','taxamt','assdtotalvalue','prevsalerecordingdate','prevsalecontractdate'
  ];
begin
  -- ---- WHERE: only the supplied filters (keeps the plan index-friendly) ----
  if p_fips  is not null then conds := conds || ' and p.fips = '       || quote_literal(p_fips);         end if;
  if p_apn   is not null then conds := conds || ' and p.apn = '        || quote_literal(p_apn);          end if;
  if p_state is not null then conds := conds || ' and p.situsstate = ' || quote_literal(upper(p_state)); end if;
  if p_zip   is not null then conds := conds || ' and p.situszip5 = '  || quote_literal(p_zip);          end if;

  if p_yearbuilt_min is not null then conds := conds || ' and p.yearbuilt ~ ' || quote_literal('^[0-9]{4}$') || ' and p.yearbuilt::int >= ' || p_yearbuilt_min::text; end if;
  if p_yearbuilt_max is not null then conds := conds || ' and p.yearbuilt ~ ' || quote_literal('^[0-9]{4}$') || ' and p.yearbuilt::int <= ' || p_yearbuilt_max::text; end if;

  if p_taxamt_min is not null then conds := conds || ' and p.taxamt ~ ' || quote_literal('^[0-9]+([.][0-9]+)?$') || ' and p.taxamt::numeric >= ' || p_taxamt_min::text; end if;
  if p_taxamt_max is not null then conds := conds || ' and p.taxamt ~ ' || quote_literal('^[0-9]+([.][0-9]+)?$') || ' and p.taxamt::numeric <= ' || p_taxamt_max::text; end if;

  if p_assdvalue_min is not null then conds := conds || ' and p.assdtotalvalue ~ ' || quote_literal('^[0-9]+([.][0-9]+)?$') || ' and p.assdtotalvalue::numeric >= ' || p_assdvalue_min::text; end if;
  if p_assdvalue_max is not null then conds := conds || ' and p.assdtotalvalue ~ ' || quote_literal('^[0-9]+([.][0-9]+)?$') || ' and p.assdtotalvalue::numeric <= ' || p_assdvalue_max::text; end if;

  if p_recdate_from is not null then conds := conds || ' and p.prevsalerecordingdate <> '''' and p.prevsalerecordingdate >= ' || quote_literal(p_recdate_from); end if;
  if p_recdate_to   is not null then conds := conds || ' and p.prevsalerecordingdate <> '''' and p.prevsalerecordingdate <= ' || quote_literal(p_recdate_to);   end if;
  if p_contractdate_from is not null then conds := conds || ' and p.prevsalecontractdate <> '''' and p.prevsalecontractdate >= ' || quote_literal(p_contractdate_from); end if;
  if p_contractdate_to   is not null then conds := conds || ' and p.prevsalecontractdate <> '''' and p.prevsalecontractdate <= ' || quote_literal(p_contractdate_to);   end if;

  -- ---- SELECT: which columns to return ----
  if p_all_fields then
    sel := 'to_jsonb(p)';                     -- the whole row (all 229 columns)
  else
    -- validate any requested extra fields against the real column list
    select array_agg(column_name::text) into valid_cols
      from information_schema.columns
      where table_schema = 'public' and table_name = 'properties';

    want_cols := default_cols;
    if p_fields is not null then
      want_cols := want_cols || coalesce((
        select array_agg(lower(f))
        from unnest(p_fields) f
        where lower(f) = any(valid_cols) and lower(f) <> all(default_cols)
      ), '{}'::text[]);
    end if;

    -- jsonb_build_object('col', p."col", ...)  — %L key, %I identifier (both safe)
    select string_agg(format('%L, p.%I', c, c), ', ') into sel from unnest(want_cols) c;
    sel := 'jsonb_build_object(' || sel || ')';
  end if;

  q := 'select ' || sel || ' from public.properties p where ' || conds
    || ' order by p.propertyid limit ' || nlim::text || ' offset ' || noff::text;

  return query execute q;
end;
$func$;

grant execute on function public.search_properties(
  text, text, text, text, int, int, numeric, numeric, numeric, numeric,
  text, text, text, text, text[], boolean, int, int
) to service_role;

create index if not exists properties_state_pid_idx on public.properties (situsstate, propertyid);
create index if not exists properties_fips_pid_idx  on public.properties (fips, propertyid);
create index if not exists properties_apn_idx        on public.properties (apn);
