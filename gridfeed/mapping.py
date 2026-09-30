"""Turn a raw device message into measurements, driven only by a mapping file.

Every vendor sends something different: named JSON readings in kW, numeric
channel codes with integer raw values, IEC 104 information object addresses,
a German CSV with decimal commas. The code here knows the shapes; which field
means what, in which unit and scale, lives in mappings/*.yaml. Connecting a new
device type means a new mapping file plus a test against recorded raw messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
QUANTITIES = ("active_power", "reactive_power", "voltage", "current")


@dataclass
class Measurement:
    ts: datetime
    device: str
    quantity: str
    value: float  # SI unit
    unit: str
    integration: str
    source_flags: list[str] = field(default_factory=list)  # e.g. IEC 104 quality bits


@dataclass
class Mapping:
    integration: str
    format: str
    channels: dict[str, dict]
    device_field: str | None = None
    timestamp: dict | None = None
    values_field: str | None = None
    delimiter: str = ","
    decimal: str = "."

    @classmethod
    def load(cls, path: str | Path) -> "Mapping":
        raw = yaml.safe_load(Path(path).read_text())
        raw["channels"] = {str(k): v for k, v in raw["channels"].items()}
        for key, ch in raw["channels"].items():
            if ch["quantity"] not in QUANTITIES:
                raise ValueError(f"{path}: channel {key} has unknown quantity {ch['quantity']!r}")
        return cls(**raw)

    def _ts(self, raw: object) -> datetime:
        fmt = (self.timestamp or {}).get("format")
        if fmt == "epoch_ms":
            return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            raise ValueError(f"timestamp without time zone: {raw!r}")
        return ts

    def _channel(self, key: str, raw: object, ts: datetime, device: str, flags=()) -> Measurement | None:
        ch = self.channels.get(str(key))
        if ch is None:
            return None  # unmapped channel: ignored, counted by the caller
        if isinstance(raw, str):
            raw = raw.replace(self.decimal, ".") if self.decimal != "." else raw
        return Measurement(ts, device, ch["quantity"], float(raw) * ch["scale"], ch["unit"], self.integration, list(flags))

    def parse(self, msg: dict) -> tuple[str, list[Measurement], list[str]]:
        """Returns device id, measurements, and keys that had no mapping."""
        device = str(msg[self.device_field])
        ts = self._ts(msg[self.timestamp["field"]])
        if self.format == "json":
            items = msg[self.values_field].items()
        elif self.format == "code_list":
            items = [(str(code), value) for code, value in msg[self.values_field]]
        elif self.format == "csv":
            items = [(k, v) for k, v in msg.items() if k not in (self.device_field, self.timestamp["field"])]
        else:
            raise ValueError(f"parse() does not handle format {self.format!r}")
        out, unmapped = [], []
        for key, raw in items:
            if raw in (None, ""):
                continue  # an empty cell is a missing value, caught by the completeness check
            m = self._channel(key, raw, ts, device)
            (out.append(m) if m else unmapped.append(str(key)))
        return device, out, unmapped

    def from_iec104(self, device: str, ioa: int, value: float, ts: datetime, flags: list[str]) -> Measurement | None:
        return self._channel(str(ioa), value, ts, device, flags)


def load_all(folder: Path = ROOT / "mappings") -> dict[str, Mapping]:
    maps = [Mapping.load(p) for p in sorted(folder.glob("*.yaml"))]
    return {m.integration: m for m in maps}


def load_devices(path: Path = ROOT / "config" / "devices.yaml") -> dict[str, dict]:
    return {str(k): v for k, v in yaml.safe_load(path.read_text())["devices"].items()}
