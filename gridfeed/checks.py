"""Two-stage quality control for measurements.

Stage 1, pre-checks on each stream, no grid model needed: the source's own
quality bits, physical plausibility, frozen values, sudden steps, gaps, time
order, and whether every expected channel arrived.

Stage 2, against the grid model: does the mapped asset exist at this voltage
level, does the measured busbar voltage match what the transformer's impedance
predicts for the measured power flow, is the current consistent with power and
voltage, and does the device's voltage fingerprint (how much voltage moves per
kW) match the transformer it is mapped to. Stage 2 also derives transformer
loading, which inherits the worst grade of its inputs, and raises overload
events only on trusted data.

Grades: 0 good, 1 suspect, 2 bad.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .grid import Transformer
from .mapping import QUANTITIES, Measurement

GOOD, SUSPECT, BAD = 0, 1, 2
GRADE_NAME = {GOOD: "good", SUSPECT: "suspect", BAD: "bad"}

EXPECTED_INTERVAL = timedelta(minutes=15)
FROZEN_RUN = 8  # identical readings in a row (2 hours) before a value counts as frozen
STEP_LIMIT = {"voltage": 0.04}  # largest plausible change between two readings, relative
MODEL_TOLERANCE = 0.015  # measured vs model-predicted busbar voltage, relative to nominal
CURRENT_TOLERANCE = 0.05
FINGERPRINT_WINDOW = 96  # readings used to fit the voltage fingerprint (one day)
FINGERPRINT_BAND = (0.5, 2.0)  # measured / model sensitivity ratio still counted as consistent


@dataclass
class Finding:
    check: str
    stage: int
    grade: int
    quantity: str | None
    detail: str


@dataclass
class Row:
    """One stored value with its grade and the reasons for it."""

    m: Measurement
    asset: str | None
    grade: int = GOOD
    flags: list[str] = field(default_factory=list)

    def downgrade(self, grade: int, flag: str) -> None:
        self.grade = max(self.grade, grade)
        if flag not in self.flags:
            self.flags.append(flag)


class StreamState:
    """Stage 1 memory for one (device, quantity) stream."""

    def __init__(self) -> None:
        self.last_ts: datetime | None = None
        self.last_value: float | None = None
        self.same = 0
        self.seen: deque[datetime] = deque(maxlen=2000)

    def check(self, row: Row, limits: dict) -> list[Finding]:
        m, out = row.m, []
        q = m.quantity
        if "IV" in m.source_flags:
            out.append(Finding("source_invalid", 1, BAD, q, "device marked the value invalid (IV)"))
        if "NT" in m.source_flags:
            out.append(Finding("source_not_topical", 1, SUSPECT, q, "device marked the value not topical (NT)"))
        lo, hi = limits.get(q, (-math.inf, math.inf))
        if not lo <= m.value <= hi:
            out.append(Finding("implausible", 1, BAD, q, f"{m.value:.2f} {m.unit} outside plausible range {lo:g} to {hi:g}"))
        if self.last_ts is not None:
            if m.ts <= self.last_ts:
                # Reported once per device, not per channel: the whole reading is repeated or late.
                if m.ts in self.seen:
                    out.append(Finding("duplicate", 1, BAD, None, f"reading for {m.ts.isoformat()} received again"))
                else:
                    out.append(Finding("out_of_order", 1, BAD, None, f"reading for {m.ts.isoformat()} arrived after {self.last_ts.isoformat()}"))
                return out
            if m.ts - self.last_ts > 2 * EXPECTED_INTERVAL:
                missing = int((m.ts - self.last_ts) / EXPECTED_INTERVAL) - 1
                out.append(Finding("gap", 1, GOOD, q, f"{missing} readings missing before {m.ts.isoformat()}"))
            if self.last_value is not None:
                self.same = self.same + 1 if m.value == self.last_value else 0
                if self.same >= FROZEN_RUN - 1:
                    out.append(Finding("frozen", 1, SUSPECT, q, f"value {m.value:g} unchanged for {self.same + 1} readings"))
                rel = STEP_LIMIT.get(q)
                if rel and self.last_value and abs(m.value - self.last_value) > rel * abs(self.last_value):
                    out.append(Finding("step", 1, SUSPECT, q, f"jumped from {self.last_value:.2f} to {m.value:.2f} {m.unit}"))
        self.last_ts, self.last_value = m.ts, m.value
        self.seen.append(m.ts)
        return out


class Fingerprint:
    """How much the busbar voltage moves per kW, fitted from a device's own data.

    U = a + b_p * P + b_q * Q by least squares over a window. A transformer's
    sensitivity follows from its rating and impedance, so a device mapped to the
    wrong transformer shows a slope several times off, even when every single
    reading looks plausible.
    """

    def __init__(self, window: int = FINGERPRINT_WINDOW) -> None:
        self.rows: deque[tuple[float, float, float]] = deque(maxlen=window)

    def add(self, p: float, q: float, u: float) -> None:
        self.rows.append((p, q, u))

    def slope(self, min_spread_w: float) -> float | None:
        if len(self.rows) < self.rows.maxlen // 2:
            return None
        n = len(self.rows)
        ps, qs, us = zip(*self.rows)
        mp, mq, mu = sum(ps) / n, sum(qs) / n, sum(us) / n
        spp = sum((p - mp) ** 2 for p in ps)
        if math.sqrt(spp / n) < min_spread_w:
            return None  # not enough variation in power to tell transformers apart
        sqq = sum((q - mq) ** 2 for q in qs)
        spq = sum((p - mp) * (q - mq) for p, q in zip(ps, qs))
        spu = sum((p - mp) * (u - mu) for p, u in zip(ps, us))
        squ = sum((q - mq) * (u - mu) for q, u in zip(qs, us))
        det = spp * sqq - spq**2
        if abs(det) < 1e-9:
            return spu / spp
        return (spu * sqq - squ * spq) / det


def model_slope(t: Transformer) -> float:
    """Model sensitivity dU/dP in volts per watt (negative: voltage falls with import)."""
    return -t.vn_lv_v * t.mv_setpoint_pu * (t.vkr_percent / 100) / (t.sn_kva * 1000)


@dataclass
class Event:
    device: str
    asset: str
    kind: str
    ts: datetime
    value: float
    detail: str


def stage2(
    rows: list[Row], asset: Transformer | None, asset_id: str | None, fp: Fingerprint
) -> tuple[list[Finding], list[Row], list[Event]]:
    """Checks one timestamp's readings of one device against the grid model.

    Returns findings, derived rows (apparent power and loading) and grid events.
    """
    findings: list[Finding] = []
    if asset is None:
        findings.append(Finding("unknown_asset", 2, BAD, None, f"mapped asset {asset_id!r} is not in the grid model"))
        for r in rows:
            r.downgrade(BAD, "unknown_asset")
        return findings, [], []
    by_q = {r.m.quantity: r for r in rows}
    missing = [q for q in QUANTITIES if q not in by_q]
    if missing:
        findings.append(Finding("missing_channel", 1, SUSPECT, None, f"no {', '.join(missing)} in this message"))

    p, qv, u, i = (by_q.get(k) for k in ("active_power", "reactive_power", "voltage", "current"))
    if u and u.grade < BAD and abs(u.m.value / asset.vn_lv_v - 1) > 0.2:
        findings.append(
            Finding("voltage_level", 2, BAD, "voltage", f"{u.m.value:.0f} V does not fit a {asset.vn_lv_v:.0f} V asset")
        )
        u.downgrade(BAD, "voltage_level")

    if u and p and qv and max(u.grade, p.grade, qv.grade) == GOOD:
        expected = asset.expected_lv_voltage(p.m.value, qv.m.value)
        dev = u.m.value - expected
        if abs(dev) > MODEL_TOLERANCE * asset.vn_lv_v:
            findings.append(
                Finding(
                    "model_voltage",
                    2,
                    SUSPECT,
                    "voltage",
                    f"measured {u.m.value:.1f} V, model predicts {expected:.1f} V for this power flow ({dev:+.1f} V)",
                )
            )
            u.downgrade(SUSPECT, "model_voltage")
        else:
            # Only readings that agree with the model feed the fingerprint, so a
            # calibration offset is reported as one and not mistaken for a mapping error.
            fp.add(p.m.value, qv.m.value, u.m.value)
        measured = fp.slope(min_spread_w=0.05 * asset.sn_kva * 1000)
        if measured is not None:
            ratio = measured / model_slope(asset)
            if not FINGERPRINT_BAND[0] <= ratio <= FINGERPRINT_BAND[1]:
                for r in (p, qv, u, i):
                    if r:
                        r.downgrade(SUSPECT, "mapping_fingerprint")
                findings.append(
                    Finding(
                        "mapping_fingerprint",
                        2,
                        SUSPECT,
                        None,
                        f"voltage moves {abs(measured) * 1e3:.3f} V/kW, the mapped {asset.sn_kva:.0f} kVA transformer "
                        f"should give {abs(model_slope(asset)) * 1e3:.3f} V/kW ({ratio:.1f}x): likely mapped to the wrong asset",
                    )
                )

    derived, events = [], []
    if p and qv:
        s = math.hypot(p.m.value, qv.m.value)
        if u and i and max(u.grade, i.grade) == GOOD and u.m.value > 0:
            i_calc = s / (math.sqrt(3) * u.m.value)
            if i_calc > 1 and abs(i.m.value - i_calc) > CURRENT_TOLERANCE * max(i_calc, 10):
                findings.append(
                    Finding("current_consistency", 2, SUSPECT, "current", f"measured {i.m.value:.1f} A, P, Q and U give {i_calc:.1f} A")
                )
                i.downgrade(SUSPECT, "current_consistency")
        grade = max(p.grade, qv.grade)
        inherited = [f"from {r.m.quantity}: {f}" for r in (p, qv) for f in r.flags]
        for quantity, value, unit in (("apparent_power", s, "VA"), ("loading", 100 * s / (asset.sn_kva * 1000), "%")):
            m = Measurement(p.m.ts, p.m.device, quantity, value, unit, p.m.integration)
            derived.append(Row(m, p.asset, grade, list(inherited)))
        loading = derived[-1]
        if loading.m.value > 100:
            if grade == GOOD:
                direction = "reverse flow (export to MV)" if p.m.value < 0 else "import"
                events.append(Event(p.m.device, asset.asset_id, "overload", p.m.ts, loading.m.value, f"{loading.m.value:.0f}% of {asset.sn_kva:.0f} kVA, {direction}"))
            else:
                findings.append(
                    Finding("overload_unconfirmed", 2, SUSPECT, "loading", f"{loading.m.value:.0f}% computed from {GRADE_NAME[grade]} inputs, not raised as an event")
                )
    return findings, derived, events
