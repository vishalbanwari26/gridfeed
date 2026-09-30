"""Golden tests: each mapping file against a recorded raw message from that source."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gridfeed.mapping import load_all, load_devices

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "raw_messages.json").read_text())
MAPS = load_all()


@pytest.mark.parametrize("integration", sorted(FIXTURES))
def test_mapping_matches_recorded_message(integration):
    case = FIXTURES[integration]
    device, ms, unmapped = MAPS[integration].parse(case["raw"])
    exp = case["expected"]
    assert device == exp["device"]
    assert sorted(unmapped) == sorted(exp.get("unmapped", []))
    assert {m.quantity: round(m.value, 6) for m in ms} == pytest.approx(exp["values"])
    assert all(m.ts == datetime.fromisoformat(exp["ts"]) for m in ms)


def test_iec104_mapping_scales_kilowatts():
    m = MAPS["iec104-rtu"].from_iec104("RTU-CA6", 1001, 88.1, datetime(2016, 6, 9, tzinfo=timezone.utc), ["NT"])
    assert m.quantity == "active_power" and m.value == pytest.approx(88100.0) and m.source_flags == ["NT"]
    assert MAPS["iec104-rtu"].from_iec104("RTU-CA6", 1999, 1.0, datetime(2016, 6, 9, tzinfo=timezone.utc), []) is None


def test_timestamp_without_zone_is_rejected():
    raw = dict(FIXTURES["mqtt-sensorco"]["raw"], time="2016-06-09T12:00:00")
    with pytest.raises(ValueError):
        MAPS["mqtt-sensorco"].parse(raw)


def test_every_device_points_at_a_known_integration():
    for dev, reg in load_devices().items():
        assert reg["integration"] in MAPS, dev
