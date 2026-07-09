#!/usr/bin/env python3
"""
loader.py -- Bulk loader for The Warren Group (TWG) weekly per-state property
             drops into ONE unified Supabase Postgres table.

WHAT IT DOES
------------
  1. Pulls the weekly TAB-delimited (TSV), 229-column, CRLF property files from a
     SWAPPABLE source (LocalSource today; SFTPSource / GCSSource seams provided).
  2. Reads each file's header row and DYNAMICALLY builds an all-TEXT staging table
     whose column set matches the file exactly (nothing is hard-coded to 229).
  3. Streams the file into staging with psycopg 3 COPY -- block by block, so a
     1.8 GB file never lands in memory.
  4. Upserts staging -> one unified main table on the composite key
     (fips, propertyid), so weekly full-file re-drops are idempotent.
  5. Ensures the main table, its composite PK, and B-tree indexes on
     (situsstate, situszip5) exist before the first upsert.
  6. Processes files smallest-first (DC before AR) and logs rows + timing per file.

WHY THESE COPY MECHANICS (grounded in the real files under d:\\Warren\\data\\)
---------------------------------------------------------------------------------
  * The data is TAB-delimited, 229 columns, CRLF, plain ASCII, header row present.
  * The big AR file contains LITERAL backslashes (\\) and double-quotes (") as
    ordinary mid-field text (names, legal descriptions, APNs).
      - FORMAT text would treat \\ as an escape char  -> corruption / errors.
      - FORMAT csv with default quoting treats " as a field quote -> parse errors
        against the CRLF endings.
    => We use FORMAT csv but redefine QUOTE to a control byte (0x01) that CANNOT
       occur in this ASCII data, so " is plain data. In CSV mode backslash is NOT
       special at all, so the literal backslashes in AR are already safe.
       CSV format also strips the CRLF terminator natively.
  * Empty fields between tabs are frequent -> NULL '' turns them into SQL NULL.
  * HEADER true discards the header row on every file.
  * ENCODING 'LATIN1' accepts every byte 0x00-0xFF, so a stray accented byte in an
    owner name cannot abort a multi-million-row COPY (matches the latin-1 header
    decode). Columns are loaded as TEXT so no cell can abort the load; type
    tightening is a later (post-POC) concern.

TRANSACTION SHAPE (disk / WAL safety on the SMALL tier)
-------------------------------------------------------
  Per file the work is split into THREE committed transactions rather than one:
     1) DDL + COPY into UNLOGGED staging  -> commit (retire the COPY's txn early)
     2) INSERT ... ON CONFLICT upsert     -> commit
     3) DROP staging, then VACUUM (ANALYZE) main in autocommit
  A single giant COPY + 2.3M-row upsert + DROP in ONE transaction on the AR file
  holds WAL and dead tuples for the whole multi-minute run and can disk-full or
  time out on the 8 GB default disk during an upsert RE-load (every row rewritten
  as a dead tuple). Splitting the txns lets WAL retire between phases and the
  VACUUM reclaims the re-load bloat.

POOLER REQUIREMENT
------------------
  The session SETs (statement_timeout, etc.) only persist on the SESSION pooler
  (port 5432) or a direct connection -- NOT the TRANSACTION pooler (port 6543),
  which resets session state per transaction. connect() probes the effective
  statement_timeout after the SETs and FAILS FAST if it didn't stick, so the
  1.8 GB AR COPY can't silently run under Supabase's short default timeout.

USAGE (see the 2-line note beneath the code block)
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import Iterable, Iterator, List, Optional

import psycopg  # psycopg 3  (pip install "psycopg[binary]>=3.2")
from psycopg import sql

# Optionally load repo-root .env (twg-api/.env) so SUPABASE_DSN etc. don't have to
# be exported in every shell. python-dotenv is optional; real shell env vars still
# work and take precedence over .env values.
try:
    from dotenv import load_dotenv

    load_dotenv(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    )
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Configuration / constants
# ---------------------------------------------------------------------------

# Default location of the already-downloaded, unzipped weekly files.
DEFAULT_LOCAL_DIR = r"d:\Warren\data"

# Only files matching this shape are loaded from a source.
FILE_PREFIX = "Weekly_"
FILE_SUFFIX = ".txt"

# The unified main table and the reusable staging table.
MAIN_TABLE = "properties"
STAGING_TABLE = "properties_staging"

# Composite key for idempotent re-loads and the endpoint's filter columns.
# Identifiers are lower-cased (see normalize_ident); these are the lower-cased forms.
KEY_COLUMNS = ("fips", "propertyid")          # (FIPS, PropertyID)
FILTER_COLUMNS = ("situsstate", "situszip5")  # POC endpoint filters state + zip

# COPY streaming block size. 8 MiB keeps memory flat regardless of file size.
COPY_BLOCK_BYTES = 8 << 20  # 8 MiB

# Rough state order (smallest-first) used ONLY as a tiebreaker when a source can't
# report file size (e.g. some SFTP servers). Real byte size is preferred.
STATE_ORDER_HINT = ["DC", "VT", "WY", "RI", "DE", "SD", "NH", "HI", "ND", "AR"]

log = logging.getLogger("twg.loader")


# ===========================================================================
# SWAPPABLE SOURCE ABSTRACTION
# ===========================================================================
#
# A Source lists filenames and opens them as *binary, streaming* file-like
# objects with a working .read(size) method. The loader only ever calls
# .read(size) in a loop, so any source that can produce a binary stream (local
# disk, SFTP, GCS) drops in with no change to the COPY path.
# ===========================================================================


class Source(ABC):
    """Abstract file source. Implementations must yield binary streams."""

    @abstractmethod
    def list_files(self) -> List[str]:
        """Return the bare filenames (Weekly_*.txt) available from this source."""

    @abstractmethod
    def open(self, name: str):
        """Return a context manager yielding a binary, streaming file-like for `name`."""
        raise NotImplementedError

    def size_of(self, name: str) -> int:
        """Best-effort size in bytes, used only to order smallest-first.

        Return -1 if unknown; unknown-size files fall back to STATE_ORDER_HINT.
        """
        return -1


class LocalSource(Source):
    """Reads the already-downloaded files from a local folder (default data)."""

    def __init__(self, folder: str = DEFAULT_LOCAL_DIR):
        self.folder = folder

    def list_files(self) -> List[str]:
        if not os.path.isdir(self.folder):
            raise FileNotFoundError(f"Local source folder not found: {self.folder}")
        return [
            n
            for n in os.listdir(self.folder)
            if n.startswith(FILE_PREFIX) and n.endswith(FILE_SUFFIX)
        ]

    @contextmanager
    def open(self, name: str) -> Iterator[io.BufferedReader]:
        path = os.path.join(self.folder, name)
        # Binary mode: we do our own byte streaming into COPY; no text decoding.
        f = open(path, "rb", buffering=1024 * 1024)
        try:
            yield f
        finally:
            f.close()

    def size_of(self, name: str) -> int:
        try:
            return os.path.getsize(os.path.join(self.folder, name))
        except OSError:
            return -1


class SFTPSource(Source):
    """SFTP source stub (host dataftp.thewarrengroup.com, port 22).

    Fill in with paramiko when SFTP delivery goes live. Credentials come from env:
        TWG_SFTP_HOST (default dataftp.thewarrengroup.com)
        TWG_SFTP_PORT (default 22)
        TWG_SFTP_USER
        TWG_SFTP_PASS
        TWG_SFTP_DIR  (remote directory, default ".")
    The rest of the loader is unchanged: .open() returns a binary stream and
    paramiko's SFTPFile.read(size) streams straight into COPY.
    """

    def __init__(self):
        self.host = os.environ.get("TWG_SFTP_HOST", "dataftp.thewarrengroup.com")
        self.port = int(os.environ.get("TWG_SFTP_PORT", "22"))
        self.user = os.environ.get("TWG_SFTP_USER", "")
        self.password = os.environ.get("TWG_SFTP_PASS", "")
        self.remote_dir = os.environ.get("TWG_SFTP_DIR", ".")
        self._sftp = None  # lazily created paramiko.SFTPClient

    def _connect(self):
        if self._sftp is not None:
            return self._sftp
        # --- Uncomment and complete when going live ---------------------------
        # import paramiko
        # transport = paramiko.Transport((self.host, self.port))
        # transport.connect(username=self.user, password=self.password)
        # self._sftp = paramiko.SFTPClient.from_transport(transport)
        # return self._sftp
        raise NotImplementedError(
            "SFTPSource is a stub. Install paramiko and complete _connect()/open(). "
            "Set TWG_SFTP_USER / TWG_SFTP_PASS (and optionally TWG_SFTP_HOST/PORT/DIR)."
        )

    def list_files(self) -> List[str]:
        sftp = self._connect()
        return [
            n
            for n in sftp.listdir(self.remote_dir)  # type: ignore[union-attr]
            if n.startswith(FILE_PREFIX) and n.endswith(FILE_SUFFIX)
        ]

    @contextmanager
    def open(self, name: str) -> Iterator[io.BufferedReader]:
        sftp = self._connect()
        remote_path = f"{self.remote_dir.rstrip('/')}/{name}"
        f = sftp.open(remote_path, "rb")  # type: ignore[union-attr]
        try:
            # Larger prefetch/window helps SFTP throughput for multi-GB files.
            f.prefetch()  # paramiko SFTPFile optimization; safe to keep
        except Exception:  # pragma: no cover - not all servers support it
            pass
        try:
            yield f  # type: ignore[misc]
        finally:
            f.close()

    def size_of(self, name: str) -> int:
        try:
            sftp = self._connect()
            return sftp.stat(f"{self.remote_dir.rstrip('/')}/{name}").st_size  # type: ignore[union-attr]
        except Exception:
            return -1


class GCSSource(Source):  # pragma: no cover - future seam
    """Future Google Cloud Storage source (SEAM ONLY -- not implemented).

    Intended shape when needed:
        from google.cloud import storage
        client = storage.Client()
        bucket = client.bucket(os.environ["TWG_GCS_BUCKET"])
        # list_files(): [b.name for b in bucket.list_blobs(prefix=...) if match]
        # open(name):   bucket.blob(name).open("rb")  -> binary streaming reader
    Because .open() returns a binary .read(size) stream, the COPY path is identical
    to LocalSource/SFTPSource; only these three methods change.
    """

    def __init__(self):
        raise NotImplementedError(
            "GCSSource is a future seam. Implement list_files()/open()/size_of() "
            "using google-cloud-storage; the COPY path stays the same."
        )

    def list_files(self) -> List[str]:
        raise NotImplementedError

    @contextmanager
    def open(self, name: str) -> Iterator[io.BufferedReader]:
        raise NotImplementedError
        yield  # pragma: no cover


def build_source(kind: str, local_dir: str) -> Source:
    kind = kind.lower()
    if kind == "local":
        return LocalSource(local_dir)
    if kind == "sftp":
        return SFTPSource()
    if kind == "gcs":
        return GCSSource()
    raise ValueError(f"Unknown source kind: {kind!r} (expected local|sftp|gcs)")


# ===========================================================================
# HEADER PARSING / IDENTIFIER HANDLING
# ===========================================================================


def normalize_ident(raw: str) -> str:
    """Lower-case a header name and keep it a safe SQL identifier.

    The real headers (FIPS, PropertyID, SitusZIP5, ...) are already clean
    identifiers; we lower-case them (=> fips, propertyid, situszip5) and defensively
    map any stray non-word char to '_'. All identifiers are still double-quoted in
    SQL, so this only guards against pathological future headers.
    """
    s = raw.strip().lstrip("﻿")  # strip whitespace + any UTF-8 BOM
    out = []
    for ch in s:
        out.append(ch if (ch.isalnum() or ch == "_") else "_")
    ident = "".join(out).lower()
    if not ident:
        raise ValueError(f"Header produced an empty identifier from {raw!r}")
    if ident[0].isdigit():
        ident = "_" + ident
    return ident


def read_header_columns(source: Source, name: str) -> List[str]:
    """Read ONLY the first line of a file and return normalized column identifiers.

    Reads in small binary chunks until the first LF, so we never buffer the file.
    Handles CRLF by stripping the trailing CR before splitting on TAB.
    """
    with source.open(name) as f:
        buf = bytearray()
        while b"\n" not in buf:
            chunk = f.read(65536)
            if not chunk:
                break
            buf.extend(chunk)
    line = bytes(buf).split(b"\n", 1)[0].rstrip(b"\r")
    # Files are plain ASCII; decode leniently in case of a stray high byte.
    raw_names = line.decode("latin-1").split("\t")
    cols = [normalize_ident(c) for c in raw_names]
    if len(cols) != len(set(cols)):
        dupes = sorted({c for c in cols if cols.count(c) > 1})
        raise ValueError(f"Duplicate column identifiers after normalization: {dupes}")
    return cols


# ===========================================================================
# STREAMING COPY (constant memory, CRLF-safe via FORMAT csv)
# ===========================================================================


def stream_blocks(fobj, block: int = COPY_BLOCK_BYTES) -> Iterable[bytes]:
    """Yield raw binary blocks from a file object until EOF.

    We do NOT convert CRLF ourselves: FORMAT csv strips the \\r\\n terminator
    natively (verified against the real files), and rewriting bytes risks
    corrupting a block boundary. Constant memory: one block at a time.
    """
    while True:
        chunk = fobj.read(block)
        if not chunk:
            break
        yield chunk


def copy_file_into_staging(
    cur: psycopg.Cursor, source: Source, name: str, columns: List[str]
) -> int:
    """Stream one file into the staging table via COPY. Returns rows copied.

    The COPY options are the crux (see module docstring):
        FORMAT csv           -> strips CRLF, tolerant parser
        DELIMITER E'\\t'      -> tab separated
        HEADER true          -> discard the header row
        NULL ''              -> empty field between tabs becomes SQL NULL
        QUOTE  E'\\x01'       -> a byte absent from the data (verified 0 in DC & AR)
                                => the ~16k literal double-quotes in AR are literal.
        ESCAPE E'\\x01'       -> ESCAPE defaults to QUOTE in CSV mode, so this is a
                                no-op kept only for explicitness. In CSV mode a
                                backslash is NOT special, so the 709 literal
                                backslashes in AR are already safe regardless.
        ENCODING 'LATIN1'    -> accept every byte 0x00-0xFF; a stray accented byte
                                (e.g. an accented owner name) can't abort the load,
                                and this matches the latin-1 header decode above.
    """
    collist = sql.SQL(", ").join(sql.Identifier(c) for c in columns)
    # The E'\x01' string literals are constant, embedded directly, and safe.
    copy_stmt = sql.SQL(
        "COPY {tbl} ({cols}) FROM STDIN WITH ("
        "FORMAT csv, DELIMITER E'\\t', HEADER true, NULL '', "
        "QUOTE E'\\x01', ESCAPE E'\\x01', ENCODING 'LATIN1')"
    ).format(tbl=sql.Identifier(STAGING_TABLE), cols=collist)

    with source.open(name) as f:
        with cur.copy(copy_stmt) as cp:
            for block in stream_blocks(f):
                cp.write(block)
    # rowcount reflects rows ingested by the COPY.
    return cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0


# ===========================================================================
# DDL: staging (per-file dynamic), main table, PK, indexes, upsert
# ===========================================================================


def recreate_staging(cur: psycopg.Cursor, columns: List[str]) -> None:
    """(Re)create an UNLOGGED all-TEXT staging table matching this file's header.

    UNLOGGED skips WAL for the disposable staging table -> materially faster loads.
    Built dynamically from the header so the 229 columns are never hard-coded.
    """
    coldefs = sql.SQL(", ").join(
        sql.SQL("{} text").format(sql.Identifier(c)) for c in columns
    )
    cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(STAGING_TABLE)))
    cur.execute(
        sql.SQL("CREATE UNLOGGED TABLE {} ({})").format(
            sql.Identifier(STAGING_TABLE), coldefs
        )
    )


def ensure_main_table(cur: psycopg.Cursor, columns: List[str]) -> None:
    """Create the unified main table (all TEXT), composite PK, and filter indexes.

    Idempotent: uses IF NOT EXISTS everywhere and adds the PK only if missing.
    The main table's column set is defined by the FIRST file's header; all 10 files
    share the identical 229-column header, so this is stable across states.
    """
    # Key columns must exist and be NOT NULL to back the composite primary key.
    for kc in KEY_COLUMNS:
        if kc not in columns:
            raise ValueError(
                f"Key column {kc!r} not present in file header; cannot build PK. "
                f"Header started with: {columns[:5]}..."
            )

    coldefs = []
    for c in columns:
        not_null = sql.SQL(" NOT NULL") if c in KEY_COLUMNS else sql.SQL("")
        coldefs.append(sql.SQL("{} text{}").format(sql.Identifier(c), not_null))
    cur.execute(
        sql.SQL("CREATE TABLE IF NOT EXISTS {} ({})").format(
            sql.Identifier(MAIN_TABLE), sql.SQL(", ").join(coldefs)
        )
    )

    # Add the composite primary key only if the table has no PK yet.
    pk_name = f"{MAIN_TABLE}_pkey"
    cur.execute(
        "SELECT 1 FROM pg_constraint "
        "WHERE conrelid = %s::regclass AND contype = 'p'",
        (MAIN_TABLE,),
    )
    if cur.fetchone() is None:
        cur.execute(
            sql.SQL("ALTER TABLE {} ADD CONSTRAINT {} PRIMARY KEY ({})").format(
                sql.Identifier(MAIN_TABLE),
                sql.Identifier(pk_name),
                sql.SQL(", ").join(sql.Identifier(k) for k in KEY_COLUMNS),
            )
        )

    # B-tree index on (situsstate, situszip5) serves state-only and state+zip.
    present_filter = [c for c in FILTER_COLUMNS if c in columns]
    if len(present_filter) == len(FILTER_COLUMNS):
        idx_name = f"{MAIN_TABLE}_{'_'.join(FILTER_COLUMNS)}_idx"
        cur.execute(
            sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} ({})").format(
                sql.Identifier(idx_name),
                sql.Identifier(MAIN_TABLE),
                sql.SQL(", ").join(sql.Identifier(c) for c in FILTER_COLUMNS),
            )
        )
    else:
        log.warning(
            "Filter columns %s not all present in header; skipping filter index.",
            FILTER_COLUMNS,
        )


def upsert_staging_into_main(cur: psycopg.Cursor, columns: List[str]) -> int:
    """Idempotent upsert: staging -> main on (fips, propertyid). Returns rows affected.

    - DISTINCT ON collapses any duplicate keys WITHIN one file so ON CONFLICT can't
      touch the same row twice (guards the "cannot affect row a second time" error).
      NOTE: there is no recency column in the data, so when a future file contains
      two rows with the same (fips, propertyid) the row KEPT is arbitrary (whichever
      the scan orders first). Verified 0 intra-file dupes today, so this never fires
      on current data; documented here because it makes "idempotent" slightly
      overstated for a hypothetical dup-bearing future drop.
    - ON CONFLICT DO UPDATE overwrites every non-key column from the new drop, so a
      weekly full-file re-load is fully idempotent for present keys. This does NOT
      delete rows that vanished from a shrinking drop (no row-deletion semantics).
    """
    non_key = [c for c in columns if c not in KEY_COLUMNS]

    collist = sql.SQL(", ").join(sql.Identifier(c) for c in columns)
    keylist = sql.SQL(", ").join(sql.Identifier(c) for c in KEY_COLUMNS)
    setlist = sql.SQL(", ").join(
        sql.SQL("{col} = EXCLUDED.{col}").format(col=sql.Identifier(c)) for c in non_key
    )

    stmt = sql.SQL(
        "INSERT INTO {main} ({cols}) "
        "SELECT DISTINCT ON ({keys}) {cols} FROM {stg} "
        "ORDER BY {keys} "
        "ON CONFLICT ({keys}) DO UPDATE SET {sets}"
    ).format(
        main=sql.Identifier(MAIN_TABLE),
        cols=collist,
        keys=keylist,
        stg=sql.Identifier(STAGING_TABLE),
        sets=setlist,
    )
    cur.execute(stmt)
    return cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0


# ===========================================================================
# PER-FILE ORCHESTRATION
# ===========================================================================


def load_one_file(conn: psycopg.Connection, source: Source, name: str) -> None:
    """Load a single file end to end.

    The per-file work is split into THREE committed transactions (see the module
    docstring's TRANSACTION SHAPE note) so the AR file can't hold WAL + dead tuples
    for the whole run and disk-full / time out on the SMALL tier:
       1) DDL + COPY into UNLOGGED staging  -> commit
       2) upsert staging -> main            -> commit
       3) DROP staging, then VACUUM (ANALYZE) main in autocommit
    """
    t0 = time.perf_counter()
    columns = read_header_columns(source, name)

    # --- 1) DDL + COPY into staging, then COMMIT (retire the COPY's WAL/txn). ---
    with conn.cursor() as cur:
        ensure_main_table(cur, columns)
        recreate_staging(cur, columns)
        t_copy = time.perf_counter()
        copied = copy_file_into_staging(cur, source, name, columns)
        copy_secs = time.perf_counter() - t_copy
    conn.commit()

    # --- 2) Upsert staging -> main in its OWN transaction, then COMMIT. ---------
    with conn.cursor() as cur:
        t_up = time.perf_counter()
        affected = upsert_staging_into_main(cur, columns)
        upsert_secs = time.perf_counter() - t_up
    conn.commit()

    # --- 3) Drop staging + reclaim re-load dead-tuple bloat. --------------------
    # VACUUM cannot run inside a transaction block, so flip to autocommit for it.
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(STAGING_TABLE))
        )
    conn.commit()

    old_autocommit = conn.autocommit
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("VACUUM (ANALYZE) {}").format(sql.Identifier(MAIN_TABLE))
            )
    finally:
        conn.autocommit = old_autocommit

    total = time.perf_counter() - t0
    rows_per_s = copied / copy_secs if copy_secs > 0 else 0.0
    log.info(
        "%-32s copied=%s upserted=%s | copy=%.1fs (%s rows/s) upsert=%.1fs total=%.1fs",
        name,
        f"{copied:,}",
        f"{affected:,}",
        copy_secs,
        f"{rows_per_s:,.0f}",
        upsert_secs,
        total,
    )


def order_smallest_first(source: Source, names: List[str]) -> List[str]:
    """Order files by real byte size ascending (DC first, AR last).

    Falls back to STATE_ORDER_HINT position when a size is unknown (e.g. SFTP stat
    unsupported), so we still validate on a small file before the big ones.
    """

    def hint_rank(n: str) -> int:
        for i, st in enumerate(STATE_ORDER_HINT):
            if f"_{st}_" in n:
                return i
        return len(STATE_ORDER_HINT)

    def key(n: str):
        size = source.size_of(n)
        # Known sizes sort first (0-bucket); unknown sizes use the hint (1-bucket).
        if size >= 0:
            return (0, size, n)
        return (1, hint_rank(n), n)

    return sorted(names, key=key)


# ===========================================================================
# CONNECTION
# ===========================================================================


@contextmanager
def connect(dsn: str, timeout_seconds: int) -> Iterator[psycopg.Connection]:
    """Open a psycopg 3 connection tuned for a COPY-heavy load over the pooler.

    - prepare_threshold=None: robust through Supabase's pooler (no prepared stmts).
    - autocommit=False: each phase is committed explicitly (see load_one_file).
    - Raise statement / idle-in-transaction timeouts so multi-GB COPYs aren't killed
      by Supabase's short default statement_timeout. These SET commands only stick on
      a session-mode (port 5432) or direct connection -- NOT the transaction pooler
      (6543). A startup PROBE below fails fast if the SETs didn't persist, which is
      the tell-tale of being connected to the 6543 transaction pooler.
    """
    conn = psycopg.connect(dsn, prepare_threshold=None, autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("SET statement_timeout = {}").format(
                    sql.Literal(f"{timeout_seconds}s")
                )
            )
            cur.execute(
                sql.SQL("SET idle_in_transaction_session_timeout = {}").format(
                    sql.Literal(f"{timeout_seconds}s")
                )
            )
            # Modest maintenance_work_mem speeds PK/index maintenance on the SMALL
            # (2 GB RAM) tier without risking OOM.
            cur.execute("SET maintenance_work_mem = '256MB'")
            # Faster COPY; safe for a reloadable POC (data can always be re-sent).
            cur.execute("SET synchronous_commit = off")
        conn.commit()

        # --- Guard: prove the session SETs actually persisted. ------------------
        # On the TRANSACTION pooler (port 6543) session state silently resets per
        # transaction, so the 1.8 GB AR COPY would then run under Supabase's short
        # default statement_timeout and abort mid-load. Compare the effective value
        # numerically and fail fast if it's below what we asked for.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT setting::bigint FROM pg_settings "
                "WHERE name = 'statement_timeout'"
            )
            row = cur.fetchone()
            effective_ms = row[0] if row is not None else 0
            requested_ms = timeout_seconds * 1000
            if effective_ms < requested_ms:
                raise RuntimeError(
                    f"statement_timeout did not persist "
                    f"(effective={effective_ms}ms, requested={requested_ms}ms). "
                    "You are almost certainly connected to the TRANSACTION pooler "
                    "(port 6543), which resets session SETs per transaction. "
                    "Reconnect with the SESSION pooler (port 5432) or a direct "
                    "connection so the timeout persists for the multi-GB COPY."
                )
        conn.commit()

        yield conn
    finally:
        conn.close()


# ===========================================================================
# CLI
# ===========================================================================


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Bulk-load TWG weekly property TSVs into one Supabase table."
    )
    p.add_argument(
        "--dsn",
        default=os.environ.get("SUPABASE_DSN"),
        help="Postgres connection string. Default: env SUPABASE_DSN. "
        "Use the Supabase SESSION pooler (port 5432).",
    )
    p.add_argument(
        "--source",
        default=os.environ.get("TWG_SOURCE", "local"),
        choices=["local", "sftp", "gcs"],
        help="Where to read files from (default: local, or env TWG_SOURCE).",
    )
    p.add_argument(
        "--local-dir",
        default=os.environ.get("TWG_LOCAL_DIR", DEFAULT_LOCAL_DIR),
        help=f"Folder for the local source (default: {DEFAULT_LOCAL_DIR}).",
    )
    p.add_argument(
        "--only",
        nargs="*",
        default=None,
        metavar="STATE",
        help="Optional list of state codes to load (e.g. --only DC DE). "
        "Default: all files found.",
    )
    p.add_argument(
        "--list",
        action="store_true",
        help="Print the resolved load order (smallest-first) and exit; no DB, no load.",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=int(os.environ.get("TWG_STATEMENT_TIMEOUT", "3600")),
        help="Per-statement timeout in seconds for the load session (default 3600).",
    )
    p.add_argument("--verbose", action="store_true", help="Debug-level logging.")
    return p.parse_args(argv)


def resolve_files(source: Source, only: Optional[List[str]]) -> List[str]:
    names = source.list_files()
    if only:
        wanted = {s.upper() for s in only}
        names = [n for n in names if any(f"_{s}_" in n for s in wanted)]
    return order_smallest_first(source, names)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )

    source = build_source(args.source, args.local_dir)
    ordered = resolve_files(source, args.only)

    if not ordered:
        log.error(
            "No matching %s*%s files found from source %r.",
            FILE_PREFIX,
            FILE_SUFFIX,
            args.source,
        )
        return 1

    if args.list:
        for i, n in enumerate(ordered, 1):
            size = source.size_of(n)
            size_str = f"{size / (1 << 20):,.0f} MB" if size >= 0 else "size unknown"
            print(f"{i:2}. {n}  ({size_str})")
        return 0

    if not args.dsn:
        log.error(
            "No DSN provided. Set --dsn or the SUPABASE_DSN env var "
            "(Supabase session pooler, port 5432)."
        )
        return 2

    log.info(
        "Loading %d file(s) smallest-first from %r: %s",
        len(ordered),
        args.source,
        ", ".join(ordered),
    )

    run_start = time.perf_counter()
    with connect(args.dsn, args.timeout) as conn:
        for name in ordered:
            try:
                load_one_file(conn, source, name)
            except Exception:
                conn.rollback()
                log.exception("FAILED loading %s (transaction rolled back).", name)
                return 1

    log.info("All files loaded in %.1fs.", time.perf_counter() - run_start)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
