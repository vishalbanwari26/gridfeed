"""The live ingestor: MQTT and IEC 104 into the same pipeline.

    python -m gridfeed.ingest [--reset]

MQTT messages arrive on paho's network thread and are handed over through a
queue, so all database work happens on one thread. IEC 104 values arrive per
information object; the objects of one ASDU that share a timestamp are one
reading of one station.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import queue
from collections import defaultdict
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

from . import iec104, store
from .grid import load_model
from .mapping import load_all, load_devices
from .pipeline import Pipeline

TOPICS = {"sensorco/+/data": "mqtt-sensorco", "gridbox/+/m": "mqtt-gridbox"}


def ca_device(ca: int) -> str:
    return f"RTU-CA{ca}"


async def run(mqtt_host: str, iec_host: str, iec_port: int, reset: bool) -> None:
    conn = store.connect()
    store.init(conn, reset=reset)
    pipe = Pipeline(conn, load_model(), load_devices())
    maps = load_all()
    inbox: queue.Queue = queue.Queue()

    def on_message(_c, _u, msg):
        inbox.put((msg.topic, msg.payload, datetime.now(timezone.utc)))

    def on_connect(c, _u, _f, _rc, _p=None):
        for topic in TOPICS:
            c.subscribe(topic, qos=1)

    mq = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="gridfeed-ingest")
    mq.on_message, mq.on_connect = on_message, on_connect
    mq.connect(mqtt_host, 1883)
    mq.loop_start()

    async def drain_mqtt():
        while True:
            try:
                topic, payload, received = inbox.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.05)
                continue
            integration = next(v for k, v in TOPICS.items() if topic.startswith(k.split("/")[0]))
            try:
                device, ms, unmapped = maps[integration].parse(json.loads(payload))
            except (ValueError, KeyError, TypeError) as exc:
                print(f"unparseable message on {topic}: {exc}")
                continue
            pipe.process(device, integration, ms, unmapped, received)

    rtu_map = maps["iec104-rtu"]

    async def on_asdu(a: iec104.Asdu):
        if a.type_id != iec104.M_ME_TF_1:
            return  # interrogation replies without a time tag are not stored
        groups: dict[tuple[int, datetime], list] = defaultdict(list)
        for p in a.points:
            groups[(p.ca, p.ts)].append(p)
        for (ca, ts), pts in groups.items():
            device = ca_device(ca)
            ms = [m for p in pts if (m := rtu_map.from_iec104(device, p.ioa, p.value, ts, p.flags))]
            unmapped = [str(p.ioa) for p in pts if str(p.ioa) not in rtu_map.channels]
            pipe.process(device, "iec104-rtu", ms, unmapped)

    print(f"ingesting MQTT from {mqtt_host}:1883 and IEC 104 from {iec_host}:{iec_port}")
    await asyncio.gather(drain_mqtt(), iec104.client(iec_host, iec_port, on_asdu))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mqtt", default="localhost")
    ap.add_argument("--iec-host", default="127.0.0.1")
    ap.add_argument("--iec-port", type=int, default=2404)
    ap.add_argument("--reset", action="store_true", help="drop and recreate the tables first")
    a = ap.parse_args()
    asyncio.run(run(a.mqtt, a.iec_host, a.iec_port, a.reset))
