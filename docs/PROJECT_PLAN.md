# TWG Data Access API — Project Plan

**Prepared by:** Jay · **Status:** Draft v1 for review · July 2026

---

## 1. Objective

Deliver a production-ready **Data Access API** that serves The Warren Group's property,
sales, mortgage, and related datasets to customers and partners — **secure, fast,
filterable, versioned, paginated**, with **API-key access control**, **usage-based
billing**, and **self-service onboarding**.

The proof-of-concept has validated the core approach on the property dataset; this plan
scales that proven pattern across all National Datasets and hardens it for production.

---

## 2. Where we are today (POC complete)

- **Data pipeline:** a Python loader downloads the per-state files from FTP, streams them
  into one unified Postgres table via `COPY`, and upserts idempotently (safe re-loads, no
  duplicates). 9 states (~4.6M property rows) are loaded.
- **API endpoint (`property-search`):** live on Supabase Edge Functions, with
  - **API-key authorization** (`x-api-key` header, 401 on missing/invalid),
  - **filtering** — equality on FIPS/APN/State/ZIP and ranges on YearBuilt, TaxAmt,
    AssdTotalValue, and sale dates,
  - **pagination** — configurable page size + `nextPage`/`prevPage` links,
  - **field selection** — request specific columns or all fields,
  - **indexing** for speed (a state-wide query went from ~10s to ~20ms).
- **Security:** Row Level Security enabled on the data table.
- **Docs:** build guide + operational runbook written; project under version control.

**Takeaway:** the end-to-end pattern (load → index → search function → authenticated
endpoint) works and is repeatable. The remaining work is mostly *replication across
datasets* + *productionization*.

---

## 3. Approach

- **One repeatable pattern per dataset:** loader → Postgres table → search function →
  authenticated endpoint → speed test. Each new dataset follows the property blueprint.
- **Everything behind the authenticated gateway** — no direct database access for
  customers.
- **Incremental and testable** — each dataset and feature is delivered and validated on
  its own, so progress is visible every week.

---

## 4. Phased plan

### Phase 1 — Load all National Datasets & build their endpoints  *(next up)*
**Goal:** every dataset loaded and queryable through its own endpoint.
- Extend the loader to handle each dataset's file format/schema.
- Load each National Dataset into Supabase as it lands on FTP (Transactions/Sales,
  Preforeclosure, Listings, HOA, Involuntary Liens, LO Contact, Realtor Contact, AVM).
- Build a search endpoint per dataset following the `property-search` pattern.
- Index each for its common query fields.
- **Speed-test each with TWG's provided queries** and tune.
- **Depends on:** TWG creating the datasets and putting them on FTP *(in progress)*; a
  disk/compute-tier bump for the full national data volume.
- **Done when:** all datasets are loaded, each has a working authenticated endpoint, and
  speed targets are met on the test queries.

### Phase 2 — API standardization
**Goal:** a consistent, documented API contract across all endpoints.
- Versioned routing (`/v1/`) and a standard response envelope (status, paging, totals).
- Consistent error handling and status codes.
- A published API reference (parameters, responses, examples) for all endpoints.
- **Done when:** all endpoints share one predictable request/response shape and are
  documented.

### Phase 3 — Access management & billing foundation
**Goal:** production-grade key management and the data needed to bill.
- **Hashed API keys** + an admin flow to create, rotate, and revoke keys per customer.
- **Usage logging** — record every request (key, endpoint, parameters, records returned,
  timestamp) as the billing/reporting foundation.
- **Rate limiting & quotas** per key/plan.
- **Trial keys** — self-issued, auto-expiring (e.g. 14 days / 100 calls).
- **Reporting views** — usage summaries, near-limit alerts, billing figures.
- **Done when:** keys are securely managed, every call is logged, limits are enforced, and
  usage/billing reports are available.

### Phase 4 — Self-service & UI
**Goal:** customers and internal staff can self-serve.
- Public **signup portal** for trial/paid API keys (no developer setup needed).
- **Usage dashboards** for customers.
- **Admin UI** for non-developers to manage access and view billing reports.
- **Done when:** a customer can register, get a key, and see their usage without manual
  intervention.

### Phase 5 — Production hardening, operations & handoff
**Goal:** reliable, maintainable, documented production system.
- Separate **test and production** Supabase environments with version-controlled
  deployment.
- Automate the ingestion pipeline (e.g. move the loader to Cloud Run) and shorten the
  FTP-to-cloud window.
- Performance & caching, compute-tier sizing, and load testing at production scale.
- Optional per requirements: geospatial (radius/bounding-box) search; GraphQL endpoints.
- Complete operational documentation, staff runbook, and a structured handoff/walkthrough.
- **Done when:** the platform is running in production, tested at scale, documented, and
  handed off.

---

## 5. What I need from TWG to keep moving

- **National Datasets** created and placed on FTP (Phase 1 is gated on this).
- A decision on the **disk/compute tier** for the full national data volume.
- **Priority order** for the datasets/endpoints (which first).
- **Test queries** from Sam to validate speed.
- Any **per-dataset field lists** (which fields to filter on / return) as they're defined.

---

## 6. Cadence

- **Weekly call** with Nick and Sam to review progress, confirm priorities, and unblock
  dependencies.
- Written status + hours summary per working day.

---

*This is a working draft — happy to adjust the phasing, priorities, or level of detail
based on what you and Nick would like to see first.*
