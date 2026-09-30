"""One path for every source: map, check, grade, store, log issues.

MQTT, IEC 104 and batch files all end up here with a device id and the
measurements for one timestamp. The issue log follows the rule envelio
describes for its own imports: errors are not only logged, they are recorded
and re-checked, and an issue closes itself once the source stops producing it
(for live feeds after several clean readings, for batch files when the next
import no longer contains it).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from . import store
from .checks import BAD, GOOD, SUSPECT, Event, Finding, Fingerprint, Row, StreamState, stage2
from .grid import Transformer
from .mapping import Measurement

LIMITS = {  # stage 1 plausibility for a 0.4 kV substation busbar
    "voltage": (320.0, 480.0),
    "current": (0.0, 3000.0),
    "active_power": (-1.5e6, 1.5e6),
    "reactive_power": (-1.5e6, 1.5e6),
}
CLOSE_AFTER = 4  # clean readings in a row before a live issue closes itself


class IssueLog:
    def __init__(self, conn) -> None:
        self.conn = conn
        self.open: dict[str, dict] = {}
        self.clean: dict[str, int] = defaultdict(int)
        self.seen_in_run: set[str] = set()
        self.opened = self.closed = 0
        with conn.cursor() as cur:
            cur.execute("SELECT key, integration, device, check_name FROM issue WHERE status = 'open'")
            for key, integ, dev, check in cur.fetchall():
                self.open[key] = {"integration": integ, "device": dev, "check": check}

    @staticmethod
    def key(integration: str, device: str, f: Finding) -> str:
        return f"{integration}|{device}|{f.check}|{f.quantity or ''}"

    def record(self, integration: str, device: str, f: Finding, ts: datetime) -> None:
        k = self.key(integration, device, f)
        self.seen_in_run.add(k)
        self.clean[k] = 0
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO issue (key, integration, device, check_name, quantity, stage, grade, status,
                                      first_seen, last_seen, occurrences, detail)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, 'open', %s, %s, 1, %s)
                   ON CONFLICT (key) DO UPDATE SET
                     status = 'open', closed_reason = NULL, closed_at = NULL,
                     first_seen = CASE WHEN issue.status = 'closed' THEN EXCLUDED.first_seen ELSE LEAST(issue.first_seen, EXCLUDED.first_seen) END,
                     occurrences = CASE WHEN issue.status = 'closed' THEN 1 ELSE issue.occurrences + 1 END,
                     last_seen = GREATEST(issue.last_seen, EXCLUDED.last_seen),
                     grade = GREATEST(issue.grade, EXCLUDED.grade), detail = EXCLUDED.detail""",
                (k, integration, device, f.check, f.quantity, f.stage, f.grade, ts, ts, f.detail),
            )
        if k not in self.open:
            self.opened += 1
        self.open[k] = {"integration": integration, "device": device, "check": f.check}

    def clean_reading(self, integration: str, device: str, fired: set[str]) -> None:
        """Live feeds: an open issue closes after several readings without it."""
        for k, info in list(self.open.items()):
            if info["device"] != device or info["integration"] != integration or k in fired:
                continue
            if info["check"] == "mapping_fingerprint":
                continue  # a fingerprint needs a window of data; it is re-checked, not cleared by one reading
            self.clean[k] += 1
            if self.clean[k] >= CLOSE_AFTER:
                self._close(k, f"not seen for {CLOSE_AFTER} readings")

    def close_unseen(self, integration: str, devices: set[str], import_id: int) -> None:
        """Batch files: issues from earlier imports that this import no longer shows are fixed at the source."""
        for k, info in list(self.open.items()):
            if info["integration"] == integration and info["device"] in devices and k not in self.seen_in_run:
                self._close(k, f"not reproduced in import #{import_id}")

    def _close(self, k: str, reason: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute("UPDATE issue SET status = 'closed', closed_reason = %s, closed_at = now() WHERE key = %s", (reason, k))
        self.open.pop(k, None)
        self.clean.pop(k, None)
        self.closed += 1


class Pipeline:
    def __init__(self, conn, model: dict[str, Transformer], devices: dict[str, dict], live: bool = True) -> None:
        self.conn = conn
        self.live = live  # live feeds close issues after clean readings; batch imports at the end of the import
        self.model = model
        self.devices = devices
        self.streams: dict[tuple[str, str], StreamState] = defaultdict(StreamState)
        self.fingerprints: dict[str, Fingerprint] = defaultdict(Fingerprint)
        self.issues = IssueLog(conn)
        self.open_events: dict[tuple[str, str], dict] = {}
        self.stored = 0

    def process(self, device: str, integration: str, ms: list[Measurement], unmapped: list[str] = (),
                received: datetime | None = None) -> list[Row]:
        received = received or datetime.now(timezone.utc)
        reg = self.devices.get(device)
        if reg is None or reg["integration"] != integration:
            ts = ms[0].ts if ms else received
            self.issues.record(integration, device, Finding("unknown_device", 1, BAD, None, "device is not in the registry"), ts)
            return []
        asset_id = reg["asset"]
        rows, findings = [], []
        for m in ms:
            row = Row(m, asset_id)
            fs = self.streams[(device, m.quantity)].check(row, LIMITS)
            if any(f.check in ("out_of_order", "duplicate") for f in fs):
                findings += fs
                continue
            for f in fs:
                if f.grade > GOOD:
                    row.downgrade(f.grade, f.check)
            findings += fs
            rows.append(row)
        if unmapped:
            findings.append(Finding("unmapped_channel", 1, GOOD, None, f"channels without a mapping: {', '.join(sorted(unmapped))}"))
        if not rows:
            self._log(integration, device, findings, received)
            return []
        f2, derived, events = stage2(rows, self.model.get(asset_id), asset_id, self.fingerprints[device])
        findings += f2
        rows += derived
        self._events(device, events, rows[0].m.ts)
        self._log(integration, device, findings, rows[0].m.ts)
        store.write_rows(self.conn, rows)
        self.stored += len(rows)
        store.touch_device(
            self.conn, device, integration, asset_id, reg.get("station"), received, rows[0].m.ts,
            sum(r.grade == SUSPECT for r in rows), sum(r.grade == BAD for r in rows),
        )
        return rows

    @staticmethod
    def _merge(findings: list[Finding]) -> list[Finding]:
        """A problem that hits every channel the same way (a gap, a stale flag) is one issue, not four."""
        by: dict[tuple, list[Finding]] = defaultdict(list)
        for f in findings:
            by[(f.check, f.detail if f.check != "source_not_topical" else "", f.grade)].append(f)
        out = []
        for group in by.values():
            if len(group) > 1 and len({f.quantity for f in group}) == len(group):
                g = group[0]
                out.append(Finding(g.check, g.stage, g.grade, None, g.detail + f" (all {len(group)} channels)"))
            else:
                out += group
        return out

    def _log(self, integration: str, device: str, findings: list[Finding], ts: datetime) -> None:
        findings = self._merge(findings)
        fired = set()
        for f in findings:
            self.issues.record(integration, device, f, ts)
            fired.add(IssueLog.key(integration, device, f))
        if self.live:
            self.issues.clean_reading(integration, device, fired)

    def _events(self, device: str, events: list[Event], ts: datetime) -> None:
        """Merge consecutive overload readings into one event with start, end and peak."""
        active = {e.kind for e in events}
        for e in events:
            cur = self.open_events.get((device, e.kind))
            if cur is None:
                self.open_events[(device, e.kind)] = {"e": e, "start": e.ts, "end": e.ts, "peak": e.value, "detail": e.detail}
            else:
                cur["end"] = e.ts
                if e.value > cur["peak"]:
                    cur["peak"], cur["detail"] = e.value, e.detail
            self._save_event(device, e.kind)
        for (dev, kind) in [k for k in self.open_events if k[0] == device and k[1] not in active]:
            self.open_events.pop((dev, kind))

    def _save_event(self, device: str, kind: str) -> None:
        cur = self.open_events[(device, kind)]
        with self.conn.cursor() as c:
            c.execute(
                """INSERT INTO grid_event (device, asset, kind, start_ts, end_ts, peak, detail)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (device, kind, start_ts) DO UPDATE SET end_ts = EXCLUDED.end_ts,
                     peak = EXCLUDED.peak, detail = EXCLUDED.detail""",
                (device, cur["e"].asset, kind, cur["start"], cur["end"], cur["peak"], cur["detail"]),
            )
