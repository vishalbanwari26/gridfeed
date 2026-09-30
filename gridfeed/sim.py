"""Simulated field devices: six substations, three kinds of integration, known faults.

    python -m gridfeed.sim --interval 1.0

Replays the June window of load-flow results as if it were live: each
15-minute reading is sent every `interval` seconds, over MQTT (two vendors
with different payloads) or IEC 104 (one RTU serving two stations). Sensor
noise is added so the checks do not run on perfect data. The faults below are
the ground truth the checks are expected to find.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
from datetime import datetime

import paho.mqtt.client as mqtt

from . import iec104
from .grid import ROOT

STATIONS = {
    # station: (device, transport)
    "rural1": ("SA-1001", "sensorco"),
    "rural2": ("SA-1002", "sensorco"),
    "rural3": ("GB-0301", "gridbox"),
    "semiurb4": ("GB-0402", "gridbox"),
    "semiurb5": ("RTU-CA5", "iec104"),
    "urban6": ("RTU-CA6", "iec104"),
}
CA = {"RTU-CA5": 5, "RTU-CA6": 6}

# Ground truth: step ranges are 15-minute readings from the start of the window (0..287).
FAULTS = {
    "SA-1002": [("frozen_voltage", range(96, 128))],  # voltage sensor stuck for 8 hours
    "GB-0402": [("silent", range(150, 162))],  # gateway stops sending for 3 hours
    "GB-0301": [("voltage_drift", range(144, 288)), ("new_channel", range(30, 288))],  # +2.5% after maintenance; firmware adds a channel
    "RTU-CA6": [("spike_zero", range(60, 61)), ("not_topical", range(200, 204))],  # a 0 V reading; RTU flags stale values
}


def active(device: str, step: int) -> set[str]:
    return {name for name, steps in FAULTS.get(device, []) if step in steps}


def load(station: str) -> list[dict]:
    return list(csv.DictReader(open(ROOT / "data" / "profiles" / f"{station}_live.csv")))


class Sensor:
    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.frozen_u: float | None = None

    def read(self, row: dict, faults: set[str]) -> tuple[float, float, float, float]:
        n = self.rng.gauss
        p = float(row["p_w"]) * (1 + n(0, 0.002))
        q = float(row["q_var"]) * (1 + n(0, 0.002))
        u = float(row["u_v"]) + n(0, 0.05)
        i = float(row["i_a"]) * (1 + n(0, 0.002))
        if "voltage_drift" in faults:
            u *= 1.025
        if "frozen_voltage" in faults:
            self.frozen_u = self.frozen_u if self.frozen_u is not None else round(u, 2)
            u = self.frozen_u
        else:
            self.frozen_u = None
        if "spike_zero" in faults:
            u = 0.0
        return p, q, u, i


def sensorco(device: str, ts: str, p, q, u, i) -> tuple[str, bytes]:
    payload = {"serial": device, "time": ts.replace("+00:00", "Z"),
               "readings": {"P_kW": round(p / 1000, 3), "Q_kvar": round(q / 1000, 3), "U_LL_V": round(u, 2), "I_A": round(i, 2)}}
    return f"sensorco/{device}/data", json.dumps(payload).encode()


def gridbox(device: str, ts: str, p, q, u, i, extra: bool) -> tuple[str, bytes]:
    epoch_ms = int(datetime.fromisoformat(ts).timestamp() * 1000)
    v = [[1, round(p * 10)], [2, round(q * 10)], [3, round(u * 100)], [4, round(i * 100)]]
    if extra:
        v.append([9, 2410])  # firmware update: enclosure temperature, not in the mapping
    return f"gridbox/{device}/m", json.dumps({"id": device, "t": epoch_ms, "v": v}).encode()


async def main(interval: float, mqtt_host: str, iec_port: int, start: int, steps: int) -> None:
    data = {s: load(s) for s in STATIONS}
    sensors = {dev: Sensor(k) for k, (dev, _) in enumerate(STATIONS.values())}
    mq = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="gridfeed-sim")
    mq.connect(mqtt_host, 1883)
    mq.loop_start()
    rtu = iec104.RTU()
    server = await rtu.serve(port=iec_port)
    print(f"IEC 104 RTU on :{iec_port}, MQTT to {mqtt_host}:1883, one reading every {interval}s")
    await asyncio.sleep(3)  # give the ingestor's IEC 104 client time to connect
    end = min(start + steps, len(next(iter(data.values()))))
    for step in range(start, end):
        for station, (device, transport) in STATIONS.items():
            row = data[station][step]
            faults = active(device, step)
            if "silent" in faults:
                continue
            p, q, u, i = sensors[device].read(row, faults)
            if transport == "sensorco":
                mq.publish(*sensorco(device, row["ts"], p, q, u, i), qos=1)
            elif transport == "gridbox":
                mq.publish(*gridbox(device, row["ts"], p, q, u, i, "new_channel" in faults), qos=1)
            else:
                ts = datetime.fromisoformat(row["ts"])
                qds = 0x40 if "not_topical" in faults else 0
                await rtu.publish(CA[device], [(1001, p / 1000, qds, ts), (1002, q / 1000, qds, ts), (1003, u, qds, ts), (1004, i, qds, ts)])
        if step % 16 == 0:
            print(f"  step {step:3} {data['rural1'][step]['ts']}")
        await asyncio.sleep(interval)
    await asyncio.sleep(2)
    server.close()
    mq.loop_stop()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--mqtt", default="localhost")
    ap.add_argument("--iec-port", type=int, default=2404)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--steps", type=int, default=288)
    a = ap.parse_args()
    asyncio.run(main(a.interval, a.mqtt, a.iec_port, a.start, a.steps))
