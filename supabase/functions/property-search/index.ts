// supabase/functions/property-search/index.ts
//
// TWG Property Search — POC endpoint, now with users + usage tracking.
//
// GET /property-search?<filters>&fields=&allFields=&page=&pagesize=
//   Header: x-api-key: <TWGApiKey>   (REQUIRED)
//
// New in this version (per the TWG spec tabs: UserAPIKeys / UsageLogs / ErrorReturnPackage):
//   * Every request gets a transactionID (uuid) + responseDateTime, returned in the
//     response AND written to usage_logs — so a response can be tied to its log row.
//   * Keys live in user_api_keys (joined to user_profile) with per-key limits and an
//     expiration date. Invalid/expired/inactive -> 401, TWG code 3001.
//   * Record quotas enforced: daily -> 429/3002, key-cycle -> 429/3003. Only successful
//     (code 0) responses count toward quotas. MaxRecordsPerQuery caps pagesize.
//   * ALL requests (success or error) are logged to usage_logs in the background —
//     query params as JSON, never the data payload.
//   * Errors return the spec's ErrorReturnPackage: a status-only envelope
//     { status: { version, code, msg, total:0, page:0, pagesize:0, responseDateTime, transactionID } }.
//   * Successful responses keep the shape Nick asked for (rows/page/pagesize/hasMore/
//     nextPage/prevPage) plus transactionID + responseDateTime. Switching successes to
//     the full status envelope is a decision to confirm with Mike/Nick.

import { createClient } from "jsr:@supabase/supabase-js@2";

const DEFAULT_PAGESIZE = 10;
const MAX_PAGESIZE = 100;
const API_VERSION = "1.0.0";

const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  { auth: { persistSession: false } },
);

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------
function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "x-poc": "twg-property-search" },
  });
}
function textOrNull(raw: string | null): string | null {
  if (raw === null) return null;
  const v = raw.trim();
  return v === "" ? null : v;
}
function intOrNull(raw: string | null): number | null {
  const v = textOrNull(raw);
  if (v === null) return null;
  const n = Number(v);
  return Number.isInteger(n) ? n : null;
}
function numOrNull(raw: string | null): number | null {
  const v = textOrNull(raw);
  if (v === null) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}
function dateOrNull(raw: string | null): string | null {
  const v = textOrNull(raw);
  if (v === null) return null;
  const digits = v.replace(/\D/g, "");
  return /^\d{8}$/.test(digits) ? digits : null;
}
function intParam(raw: string | null, def: number, min: number, max: number): number {
  const n = intOrNull(raw);
  if (n === null) return def;
  return Math.min(Math.max(n, min), max);
}

// ---------------------------------------------------------------------------
// usage logging (background — adds no latency; never logs the data payload)
// ---------------------------------------------------------------------------
type Ctx = {
  txId: string;
  requestedAt: string;
  apiKey: string | null;
  userId: number | null;
  params: Record<string, string>;
};

function backgroundLog(row: Record<string, unknown>): void {
  const p = supabase.from("usage_logs").insert(row)
    .then(({ error }) => {
      if (error) console.error("usage_logs insert failed:", error.message);
    })
    .catch((e) => console.error("usage_logs insert threw:", e));
  const er = (globalThis as { EdgeRuntime?: { waitUntil(p: Promise<unknown>): void } }).EdgeRuntime;
  if (er?.waitUntil) er.waitUntil(p);
  // no waitUntil available -> fire-and-forget; the promise still runs
}

/** Spec ErrorReturnPackage: status-only envelope. Logs the request, then responds. */
function errorPackage(ctx: Ctx, httpStatus: number, code: number, msg: string): Response {
  const responseDateTime = new Date().toISOString();
  backgroundLog({
    twg_transaction_id: ctx.txId,
    user_id: ctx.userId,
    twg_api_key: ctx.apiKey,
    twg_status_code: String(code),   // TWG code (1000+ range per spec)
    http_status: httpStatus,
    request_query_parm_json: ctx.params,
    returned_version: API_VERSION,
    returned_code: code,
    returned_msg: msg,
    total_matched: 0,
    page_number: 0,
    num_records_returned: 0,
    returned_date_time: responseDateTime,
    requested_date_time: ctx.requestedAt,
  });
  return json(
    {
      status: {
        version: API_VERSION,
        code,
        msg,
        total: 0,
        page: 0,
        pagesize: 0,
        responseDateTime,
        transactionID: ctx.txId,
      },
    },
    httpStatus,
  );
}

// ---------------------------------------------------------------------------
// handler
// ---------------------------------------------------------------------------
Deno.serve(async (req: Request): Promise<Response> => {
  const t0 = performance.now();
  const url = new URL(req.url);
  const q = url.searchParams;

  const ctx: Ctx = {
    txId: crypto.randomUUID(),
    requestedAt: new Date().toISOString(),
    apiKey: textOrNull(req.headers.get("x-api-key")),
    userId: null,
    params: Object.fromEntries(q.entries()),
  };

  if (req.method !== "GET") {
    return errorPackage(ctx, 405, 1005, "Method not allowed; use GET");
  }

  // ---- API key auth (spec code 3001) ----
  if (ctx.apiKey === null) {
    return errorPackage(ctx, 401, 3001, "Missing or invalid TWGApiKey");
  }
  const { data: keyInfo, error: keyErr } = await supabase.rpc("validate_api_key", {
    p_key: ctx.apiKey,
  });
  if (keyErr) {
    console.error("validate_api_key failed:", keyErr.message);
    return errorPackage(ctx, 500, 1005, "Server error");
  }
  if (keyInfo?.found) {
    ctx.userId = keyInfo.user_id; // log the user even when the key is expired/inactive
  }
  if (!keyInfo?.found || !keyInfo?.valid) {
    return errorPackage(ctx, 401, 3001, "Missing or invalid TWGApiKey");
  }

  // ---- record quotas (spec codes 3002 / 3003) ----
  if (keyInfo.used_today >= keyInfo.max_d) {
    return errorPackage(ctx, 429, 3002, "Today record Limit is Exceeded");
  }
  if (keyInfo.used_cycle >= keyInfo.max_c) {
    return errorPackage(ctx, 429, 3003, "Key cycle record Limit is Exceeded");
  }

  // ---- filters ----
  // A parameter that is PRESENT but unparseable must be a 400/5001, not silently
  // dropped (silently dropping it would return a much broader result set than the
  // caller asked for — and bill them for it).
  const invalidParams: string[] = [];
  const typed = <T>(name: string, fn: (raw: string | null) => T | null): T | null => {
    const raw = textOrNull(q.get(name));
    if (raw === null) return null;
    const v = fn(raw);
    if (v === null) invalidParams.push(name);
    return v;
  };

  const state = textOrNull(q.get("state"));
  const args: Record<string, unknown> = {
    p_fips: textOrNull(q.get("fips")),
    p_apn: textOrNull(q.get("apn")),
    p_state: state === null ? null : state.toUpperCase(),
    p_zip: textOrNull(q.get("zip")),
    p_yearbuilt_min: typed("yearBuiltMin", intOrNull),
    p_yearbuilt_max: typed("yearBuiltMax", intOrNull),
    p_taxamt_min: typed("taxAmtMin", numOrNull),
    p_taxamt_max: typed("taxAmtMax", numOrNull),
    p_assdvalue_min: typed("assdValueMin", numOrNull),
    p_assdvalue_max: typed("assdValueMax", numOrNull),
    p_recdate_from: typed("recDateFrom", dateOrNull),
    p_recdate_to: typed("recDateTo", dateOrNull),
    p_contractdate_from: typed("contractDateFrom", dateOrNull),
    p_contractdate_to: typed("contractDateTo", dateOrNull),
  };

  if (invalidParams.length > 0) {
    return errorPackage(
      ctx,
      400,
      5001,
      `Invalid Parameter Combinations. Unparseable value for: ${invalidParams.join(", ")}`,
    );
  }

  // Require at least one filter (spec code 5001: invalid parameter combination).
  if (!Object.values(args).some((v) => v !== null)) {
    return errorPackage(
      ctx,
      400,
      5001,
      "Invalid Parameter Combinations. Provide at least one filter, e.g. ?state=DC",
    );
  }

  // ---- response shaping ----
  const fieldsRaw = textOrNull(q.get("fields"));
  const fields = fieldsRaw ? fieldsRaw.split(",").map((s) => s.trim()).filter(Boolean) : null;
  const allFields = (q.get("allFields") ?? "").toLowerCase() === "true";
  args.p_fields = fields;
  args.p_all_fields = allFields;

  // ---- paging (MaxRecordsPerQuery caps the page size) ----
  const maxQ = Math.max(1, Math.min(Number(keyInfo.max_q ?? MAX_PAGESIZE), MAX_PAGESIZE));
  const pagesize = intParam(q.get("pagesize"), Math.min(DEFAULT_PAGESIZE, maxQ), 1, maxQ);
  const page = intParam(q.get("page"), 1, 1, Number.MAX_SAFE_INTEGER);
  // fetch one extra row to know for certain whether a next page exists
  args.p_limit = pagesize + 1;
  args.p_offset = (page - 1) * pagesize;

  // The search function's offset is a Postgres int4: an absurd page number must be a
  // 400/5001 for the caller, not a fake 500 "Server error" in our logs.
  const MAX_OFFSET = 2_147_483_647;
  if ((args.p_offset as number) > MAX_OFFSET) {
    return errorPackage(ctx, 400, 5001, "Invalid Parameter Combinations. page is out of range");
  }

  const { data, error } = await supabase.rpc("search_properties", args);
  if (error) {
    console.error("search_properties failed:", error.message);
    return errorPackage(ctx, 500, 1005, "Server error");
  }

  const fetched = (data ?? []) as unknown[];
  const hasMore = fetched.length > pagesize;
  const rows = hasMore ? fetched.slice(0, pagesize) : fetched;

  // ---- nextPage / prevPage as public, ready-to-call URLs ----
  const publicBase = `${Deno.env.get("SUPABASE_URL")}/functions/v1/property-search`;
  const withPage = (p: number) => {
    const u = new URL(publicBase);
    q.forEach((v, k) => u.searchParams.set(k, v));
    u.searchParams.set("page", String(p));
    return u.toString();
  };
  const nextPage = hasMore ? withPage(page + 1) : null;
  const prevPage = page > 1 ? withPage(page - 1) : null;

  const responseDateTime = new Date().toISOString();
  const query_ms = Math.round(performance.now() - t0);

  // ---- usage log (success; counts toward record quotas) ----
  backgroundLog({
    twg_transaction_id: ctx.txId,
    user_id: ctx.userId,
    twg_api_key: ctx.apiKey,
    twg_status_code: "1000", // spec: 1000 = no error at the TWG gate
    http_status: 200,
    request_query_parm_json: ctx.params,
    returned_version: API_VERSION,
    returned_code: 0,
    returned_msg: "Success",
    total_matched: null, // exact totals intentionally not computed (see hasMore design)
    page_number: page,
    num_records_returned: rows.length,
    returned_date_time: responseDateTime,
    requested_date_time: ctx.requestedAt,
  });

  return json({
    rows,
    page,
    pagesize,
    hasMore,
    nextPage,
    prevPage,
    transactionID: ctx.txId,
    responseDateTime,
    query_ms,
  });
});
