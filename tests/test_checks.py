"""Stage 1 and stage 2 checks on hand-built readings, no database needed."""

from datetime import datetime, timedelta, timezone

import pytest

from gridfeed.checks import BAD, GOOD, SUSPECT, Fingerprint, Row, StreamState, model_slope, stage2
from gridfeed.grid import Transformer
from gridfeed.mapping import Measurement
from gridfeed.pipeline import LIMITS

T0 = datetime(2016, 6, 9, tzinfo=timezone.utc)
TR160 = Transformer("T160", "rural1", 160, 400, 4.0, 1.46875, 1.025)
TR630 = Transformer("T630", "urban6", 630, 400, 6.0, 1.0952, 1.025)


def m(q, v, k=0, flags=()):
    return Measurement(T0 + timedelta(minutes=15 * k), "D", q, v, "", "test", list(flags))


def reading(tr, p, q, k=0, u=None, i=None):
    u = tr.expected_lv_voltage(p, q) if u is None else u
    i = (p * p + q * q) ** 0.5 / (3**0.5 * u) if i is None else i
    return [Row(m("active_power", p, k), tr.asset_id), Row(m("reactive_power", q, k), tr.asset_id),
            Row(m("voltage", u, k), tr.asset_id), Row(m("current", i, k), tr.asset_id)]


def test_frozen_after_eight_identical_readings():
    s = StreamState()
    checks = [[f.check for f in s.check(Row(m("voltage", 409.1, k), "a"), LIMITS)] for k in range(9)]
    assert "frozen" not in sum(checks[:7], []) and "frozen" in checks[7]


def test_implausible_and_source_flags():
    s = StreamState()
    fs = s.check(Row(m("voltage", 0.0, 0, ["IV"]), "a"), LIMITS)
    assert {f.check for f in fs} == {"implausible", "source_invalid"} and max(f.grade for f in fs) == BAD


def test_gap_and_duplicate():
    s = StreamState()
    s.check(Row(m("voltage", 409, 0), "a"), LIMITS)
    assert [f.check for f in s.check(Row(m("voltage", 409.2, 5), "a"), LIMITS)] == ["gap"]
    assert [f.check for f in s.check(Row(m("voltage", 409.2, 5), "a"), LIMITS)] == ["duplicate"]


def test_consistent_reading_passes_stage2_and_derives_loading():
    rows = reading(TR160, 80_000, 10_000)
    findings, derived, events = stage2(rows, TR160, "T160", Fingerprint())
    assert findings == [] and all(r.grade == GOOD for r in rows)
    loading = [d for d in derived if d.m.quantity == "loading"][0]
    assert loading.m.value == pytest.approx(100 * (80_000**2 + 10_000**2) ** 0.5 / 160_000)


def test_voltage_offset_is_reported_against_the_model():
    rows = reading(TR160, 50_000, 5_000, u=TR160.expected_lv_voltage(50_000, 5_000) * 1.025)
    findings, _, _ = stage2(rows, TR160, "T160", Fingerprint())
    assert [f.check for f in findings] == ["model_voltage"]


def test_overload_raised_only_on_trusted_data():
    _, _, events = stage2(reading(TR160, -195_000, 10_000), TR160, "T160", Fingerprint())
    assert events and events[0].kind == "overload" and "reverse flow" in events[0].detail
    rows = reading(TR160, -195_000, 10_000)
    rows[0].downgrade(SUSPECT, "frozen")
    findings, _, events = stage2(rows, TR160, "T160", Fingerprint())
    assert not events and "overload_unconfirmed" in [f.check for f in findings]


def test_fingerprint_catches_data_mapped_to_the_wrong_transformer():
    right, wrong = Fingerprint(), Fingerprint()
    for k in range(96):
        p = 20_000 + 60_000 * ((k % 24) / 23)  # a day of varying load on the small transformer
        u = TR160.expected_lv_voltage(p, 5_000)
        right.add(p, 5_000, u)
        wrong.add(p, 5_000, u)
    assert right.slope(3_200) / model_slope(TR160) == pytest.approx(1.0, abs=0.05)
    assert wrong.slope(12_600) / model_slope(TR630) > 2  # 160 kVA behaviour, mapped to 630 kVA


def test_unknown_asset_marks_everything_bad():
    rows = reading(TR160, 1_000, 0)
    findings, derived, _ = stage2(rows, None, "missing", Fingerprint())
    assert findings[0].check == "unknown_asset" and all(r.grade == BAD for r in rows) and not derived
