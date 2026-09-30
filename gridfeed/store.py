"""TimescaleDB storage: graded measurements, the issue log, grid events, device status.

The grade and its reasons are stored next to every value, so anything reading
the data later (a load flow, a report, an analyst) can decide what to trust
without re-running the checks.
"""

from __future__ import annotations

import os
from datetime import datetime

import psycopg

DSN = os.environ.get("GRIDFEED_DSN", "postgresql://gridfeed:gridfeed@localhost:5433/gridfeed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS measurement (
    ts          timestamptz NOT NULL,
    device      text NOT NULL,
    asset       text,
    quantity    text NOT NULL,
    value       double precision NOT NULL,
    unit        text NOT NULL,
    grade       smallint NOT NULL,
    flags       text[] NOT NULL DEFAULT '{}',
    integration text NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (device, quantity, ts)
);
SELECT create_hypertable('measurement', 'ts', if_not_exists => TRUE, migrate_data => TRUE);

CREATE TABLE IF NOT EXISTS issue (
    id            serial PRIMARY KEY,
    key           text UNIQUE NOT NULL,
    integration   text NOT NULL,
    device        text NOT NULL,
    check_name    text NOT NULL,
    quantity      text,
    stage         smallint NOT NULL,
    grade         smallint NOT NULL,
    status        text NOT NULL,
    first_seen    timestamptz NOT NULL,
    last_seen     timestamptz NOT NULL,
    occurrences   integer NOT NULL,
    detail        text NOT NULL,
    closed_reason text,
    opened_at     timestamptz NOT NULL DEFAULT now(),
    closed_at     timestamptz
);

CREATE TABLE IF NOT EXISTS grid_event (
    id        serial PRIMARY KEY,
    device    text NOT NULL,
    asset     text NOT NULL,
    kind      text NOT NULL,
    start_ts  timestamptz NOT NULL,
    end_ts    timestamptz NOT NULL,
    peak      double precision NOT NULL,
    detail    text NOT NULL,
    UNIQUE (device, kind, start_ts)
);

CREATE TABLE IF NOT EXISTS device_status (
    device        text PRIMARY KEY,
    integration   text NOT NULL,
    asset         text,
    station       text,
    last_received timestamptz,
    last_ts       timestamptz,
    messages      bigint NOT NULL DEFAULT 0,
    suspect       bigint NOT NULL DEFAULT 0,
    bad           bigint NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS import_run (
    id             serial PRIMARY KEY,
    integration    text NOT NULL,
    source         text NOT NULL,
    started_at     timestamptz NOT NULL DEFAULT now(),
    rows_in        integer NOT NULL DEFAULT 0,
    values_stored  integer NOT NULL DEFAULT 0,
    issues_opened  integer NOT NULL DEFAULT 0,
    issues_closed  integer NOT NULL DEFAULT 0
);
"""


def connect(dsn: str = DSN) -> psycopg.Connection:
    return psycopg.connect(dsn, autocommit=True)


def init(conn: psycopg.Connection, reset: bool = False) -> None:
    with conn.cursor() as cur:
        if reset:
            cur.execute("DROP TABLE IF EXISTS measurement, issue, grid_event, device_status, import_run CASCADE")
        cur.execute(SCHEMA)


def write_rows(conn: psycopg.Connection, rows: list) -> None:
    """Upsert graded values. Re-importing the same timestamps replaces them."""
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO measurement (ts, device, asset, quantity, value, unit, grade, flags, integration)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (device, quantity, ts) DO UPDATE SET
                 value = EXCLUDED.value, grade = EXCLUDED.grade, flags = EXCLUDED.flags,
                 asset = EXCLUDED.asset, received_at = now()""",
            [(r.m.ts, r.m.device, r.asset, r.m.quantity, r.m.value, r.m.unit, r.grade, r.flags, r.m.integration) for r in rows],
        )


def touch_device(conn, device: str, integration: str, asset: str | None, station: str | None,
                 received: datetime, ts: datetime, suspect: int, bad: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO device_status (device, integration, asset, station, last_received, last_ts, messages, suspect, bad)
               VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s)
               ON CONFLICT (device) DO UPDATE SET
                 last_received = EXCLUDED.last_received,
                 last_ts = GREATEST(device_status.last_ts, EXCLUDED.last_ts),
                 messages = device_status.messages + 1,
                 suspect = device_status.suspect + EXCLUDED.suspect,
                 bad = device_status.bad + EXCLUDED.bad""",
            (device, integration, asset, station, received, ts, suspect, bad),
        )
