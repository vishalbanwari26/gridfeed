"""Read API and dashboard.

    uvicorn gridfeed.api:app --port 8020
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from . import store
from .grid import ROOT, load_model
from .mapping import load_devices

app = FastAPI(title="gridfeed")
QUIET_AFTER_S = float(os.environ.get("GRIDFEED_QUIET_AFTER", "5"))  # wall-clock silence before a feed counts as quiet
MODEL = load_model()
DEVICES = load_devices()
INTEGRATIONS = {
    "mqtt-sensorco": "MQTT · SensorCo JSON",
    "mqtt-gridbox": "MQTT · GridBox codes",
    "iec104-rtu": "IEC 60870-5-104 · RTU",
    "batch-export": "Batch · CSV export",
}


def q(sql: str, *args) -> list[dict]:
    with store.connect() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/api/overview")
def overview() -> dict:
    now = datetime.now(timezone.utc)
    status = {r["device"]: r for r in q("SELECT * FROM device_status")}
    issues = q("SELECT device, count(*) AS n, max(grade) AS worst FROM issue WHERE status = 'open' GROUP BY device")
    open_by = {r["device"]: r for r in issues}
    devices = []
    for dev, reg in DEVICES.items():
        s = status.get(dev)
        if s is None and reg["integration"] == "batch-export":
            continue
        silent = None if not s or not s["last_received"] else (now - s["last_received"]).total_seconds()
        state = "waiting" if s is None else ("quiet" if reg["integration"] != "batch-export" and silent > QUIET_AFTER_S else "live")
        o = open_by.get(dev, {"n": 0, "worst": 0})
        asset = MODEL.get(reg["asset"])
        devices.append({
            "device": dev, "integration": reg["integration"], "station": reg.get("station"), "asset": reg["asset"],
            "rating_kva": asset.sn_kva if asset else None, "state": state, "silent_s": silent,
            "messages": s["messages"] if s else 0, "suspect": s["suspect"] if s else 0, "bad": s["bad"] if s else 0,
            "last_ts": s["last_ts"] if s else None, "open_issues": o["n"], "worst": o["worst"],
        })
    totals = q("""SELECT (SELECT count(*) FROM issue WHERE status='open') AS open_issues,
                         (SELECT count(*) FROM issue WHERE status='closed') AS closed_issues,
                         (SELECT count(*) FROM grid_event) AS events,
                         (SELECT count(*) FROM measurement) AS values""")[0]
    return {"devices": devices, "integrations": INTEGRATIONS, "totals": totals, "quiet_after_s": QUIET_AFTER_S}


@app.get("/api/issues")
def issues() -> list[dict]:
    return q("""SELECT integration, device, check_name, quantity, stage, grade, status, first_seen, last_seen,
                       occurrences, detail, closed_reason FROM issue
                ORDER BY status = 'open' DESC, grade DESC, last_seen DESC""")


@app.get("/api/events")
def events() -> list[dict]:
    return q("SELECT device, asset, kind, start_ts, end_ts, peak, detail FROM grid_event ORDER BY start_ts")


@app.get("/api/imports")
def imports() -> list[dict]:
    return q("SELECT * FROM import_run ORDER BY id")


@app.get("/api/series/{device}")
def series(device: str) -> dict:
    reg = DEVICES.get(device)
    if reg is None:
        raise HTTPException(404, "unknown device")
    rows = q("""SELECT ts, quantity, value, grade, flags FROM measurement
                WHERE device = %s AND quantity IN ('active_power', 'reactive_power', 'voltage', 'loading')
                ORDER BY ts""", device)
    by_ts: dict[datetime, dict] = {}
    for r in rows:
        by_ts.setdefault(r["ts"], {"ts": r["ts"]})[r["quantity"]] = {"v": r["value"], "g": r["grade"], "f": r["flags"]}
    asset = MODEL.get(reg["asset"])
    points = []
    for ts, d in by_ts.items():
        p, qv = d.get("active_power"), d.get("reactive_power")
        if asset and p and qv:
            d["expected_voltage"] = asset.expected_lv_voltage(p["v"], qv["v"])
        points.append(d)
    return {"device": device, "asset": reg["asset"], "rating_kva": asset.sn_kva if asset else None, "points": points}
