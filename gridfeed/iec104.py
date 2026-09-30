"""A small IEC 60870-5-104 implementation: enough to read measured values from a substation RTU.

Covered: APCI framing (I, S and U frames), send and receive sequence numbers
with S-frame acknowledgements, STARTDT and TESTFR, station interrogation
(C_IC_NA_1), and measured values as short floats with and without a time tag
(M_ME_TF_1, type 36, and M_ME_NC_1, type 13) including the quality descriptor.
Not covered: commands other than interrogation, file transfer, redundancy
groups, TLS (IEC 62351). The client is checked against c104, an independent
implementation, in tests/interop.

Both a simulated RTU (server) and a client live here so the demo runs without
hardware.
"""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass
from datetime import datetime, timezone

START = 0x68
STARTDT_ACT, STARTDT_CON = 0x07, 0x0B
STOPDT_ACT, STOPDT_CON = 0x13, 0x23
TESTFR_ACT, TESTFR_CON = 0x43, 0x83

M_ME_NC_1, M_ME_TF_1, C_IC_NA_1 = 13, 36, 100
COT_SPONT, COT_ACT, COT_ACTCON, COT_ACTTERM, COT_INROGEN = 3, 6, 7, 10, 20
QDS_FLAGS = {0x01: "OV", 0x10: "BL", 0x20: "SB", 0x40: "NT", 0x80: "IV"}
ACK_EVERY = 8  # w: acknowledge after this many received I-frames
T2, T3 = 2.0, 20.0  # seconds: acknowledge when idle, test the link when silent (standard defaults 10 s, 20 s)


# --- encoding -----------------------------------------------------------------------


def cp56(ts: datetime) -> bytes:
    """CP56Time2a, UTC."""
    ts = ts.astimezone(timezone.utc)
    ms = ts.second * 1000 + ts.microsecond // 1000
    return struct.pack(
        "<HBBBBB", ms, ts.minute & 0x3F, ts.hour & 0x1F, (ts.day & 0x1F) | ((ts.isoweekday() & 0x07) << 5), ts.month & 0x0F, ts.year % 100
    )


def parse_cp56(b: bytes) -> datetime:
    ms, minute, hour, day, month, year = struct.unpack("<HBBBBB", b)
    return datetime(
        2000 + (year & 0x7F), month & 0x0F, day & 0x1F, hour & 0x1F, minute & 0x3F, ms // 1000, (ms % 1000) * 1000, tzinfo=timezone.utc
    )


def u_frame(code: int) -> bytes:
    return bytes([START, 4, code, 0, 0, 0])


def s_frame(nr: int) -> bytes:
    return bytes([START, 4, 0x01, 0x00, (nr << 1) & 0xFF, (nr >> 7) & 0xFF])


def i_frame(ns: int, nr: int, asdu: bytes) -> bytes:
    return bytes([START, 4 + len(asdu), (ns << 1) & 0xFF, (ns >> 7) & 0xFF, (nr << 1) & 0xFF, (nr >> 7) & 0xFF]) + asdu


def asdu_header(type_id: int, n: int, cot: int, ca: int, oa: int = 0) -> bytes:
    return struct.pack("<BBBBH", type_id, n & 0x7F, cot, oa, ca)


def ioa(addr: int) -> bytes:
    return struct.pack("<I", addr)[:3]


def measured_tf(ca: int, points: list[tuple[int, float, int, datetime]], cot: int = COT_SPONT) -> bytes:
    body = b"".join(ioa(a) + struct.pack("<fB", v, q) + cp56(t) for a, v, q, t in points)
    return asdu_header(M_ME_TF_1, len(points), cot, ca) + body


def measured_nc(ca: int, points: list[tuple[int, float, int]], cot: int = COT_INROGEN) -> bytes:
    body = b"".join(ioa(a) + struct.pack("<fB", v, q) for a, v, q in points)
    return asdu_header(M_ME_NC_1, len(points), cot, ca) + body


def interrogation(ca: int, cot: int = COT_ACT) -> bytes:
    return asdu_header(C_IC_NA_1, 1, cot, ca) + ioa(0) + bytes([20])


# --- decoding -----------------------------------------------------------------------


@dataclass
class Point:
    ca: int
    ioa: int
    value: float
    flags: list[str]
    ts: datetime | None
    cot: int
    type_id: int


@dataclass
class Asdu:
    type_id: int
    cot: int
    ca: int
    points: list[Point]


def decode_asdu(b: bytes) -> Asdu:
    type_id, vsq, cot, _oa, ca = struct.unpack("<BBBBH", b[:6])
    n, sq = vsq & 0x7F, bool(vsq & 0x80)
    cot &= 0x3F
    body, pts = b[6:], []
    if type_id in (M_ME_NC_1, M_ME_TF_1):
        size = 5 + (7 if type_id == M_ME_TF_1 else 0)
        pos = 0
        base = None
        for k in range(n):
            if not sq or k == 0:
                addr = int.from_bytes(body[pos : pos + 3], "little")
                pos += 3
                base = addr
            else:
                addr = base + k
            value, q = struct.unpack("<fB", body[pos : pos + 5])
            ts = parse_cp56(body[pos + 5 : pos + 12]) if type_id == M_ME_TF_1 else None
            pos += size
            pts.append(Point(ca, addr, value, [name for bit, name in QDS_FLAGS.items() if q & bit], ts, cot, type_id))
    return Asdu(type_id, cot, ca, pts)


async def read_frame(reader: asyncio.StreamReader) -> bytes:
    head = await reader.readexactly(2)
    if head[0] != START:
        raise ValueError(f"bad start byte {head[0]:#x}")
    return await reader.readexactly(head[1])


class Link:
    """Sequence numbers and acknowledgements for one connection."""

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.ns = self.nr = 0
        self.unacked = 0

    async def send_i(self, asdu: bytes) -> None:
        self.writer.write(i_frame(self.ns, self.nr, asdu))
        self.ns = (self.ns + 1) % 32768
        await self.writer.drain()

    async def received_i(self) -> None:
        self.nr = (self.nr + 1) % 32768
        self.unacked += 1
        if self.unacked >= ACK_EVERY:
            await self.ack()

    async def ack(self) -> None:
        if self.unacked:
            self.writer.write(s_frame(self.nr))
            self.unacked = 0
            await self.writer.drain()


# --- simulated RTU (server) ---------------------------------------------------------------


class RTU:
    """A substation RTU with one common address per station."""

    def __init__(self) -> None:
        self.latest: dict[int, dict[int, tuple[float, int]]] = {}
        self.links: list[Link] = []

    async def publish(self, ca: int, points: list[tuple[int, float, int, datetime]]) -> None:
        self.latest.setdefault(ca, {}).update({a: (v, q) for a, v, q, _ in points})
        asdu = measured_tf(ca, points)
        for link in list(self.links):
            try:
                await link.send_i(asdu)
            except (ConnectionError, RuntimeError):
                self.links.remove(link)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        link, started = Link(writer), False
        try:
            while True:
                apdu = await read_frame(reader)
                c1 = apdu[0]
                if c1 & 0x01 == 0:  # I-frame
                    await link.received_i()
                    a = decode_asdu(apdu[4:])
                    if a.type_id == C_IC_NA_1 and started:
                        cas = list(self.latest) if a.ca == 0xFFFF else [a.ca]
                        await link.send_i(interrogation(a.ca, COT_ACTCON))
                        for ca in cas:
                            pts = [(addr, v, q) for addr, (v, q) in sorted(self.latest.get(ca, {}).items())]
                            if pts:
                                await link.send_i(measured_nc(ca, pts))
                        await link.send_i(interrogation(a.ca, COT_ACTTERM))
                    await link.ack()
                elif c1 & 0x03 == 0x01:  # S-frame: the client acknowledged our frames
                    pass
                elif c1 == STARTDT_ACT:
                    writer.write(u_frame(STARTDT_CON))
                    await writer.drain()
                    started = True
                    self.links.append(link)
                elif c1 == STOPDT_ACT:
                    started = False
                    if link in self.links:
                        self.links.remove(link)
                    writer.write(u_frame(STOPDT_CON))
                    await writer.drain()
                elif c1 == TESTFR_ACT:
                    writer.write(u_frame(TESTFR_CON))
                    await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            if link in self.links:
                self.links.remove(link)
            writer.close()

    async def serve(self, host: str = "0.0.0.0", port: int = 2404) -> asyncio.base_events.Server:
        return await asyncio.start_server(self.handle, host, port)


# --- client -------------------------------------------------------------------------------


async def client(host: str, port: int, on_asdu, interrogate_ca: int = 0xFFFF, stop: asyncio.Event | None = None,
                 retry_s: float = 2.0) -> None:
    """Connect, start data transfer, interrogate, then hand every ASDU to on_asdu. Reconnects on loss."""
    stop = stop or asyncio.Event()
    while not stop.is_set():
        try:
            reader, writer = await asyncio.open_connection(host, port)
        except OSError:
            await asyncio.sleep(retry_s)
            continue
        link = Link(writer)
        try:
            writer.write(u_frame(STARTDT_ACT))
            await writer.drain()
            idle = 0
            while not stop.is_set():
                try:
                    apdu = await asyncio.wait_for(read_frame(reader), timeout=T2)
                except asyncio.TimeoutError:
                    await link.ack()  # t2: acknowledge whatever arrived before going quiet
                    idle += 1
                    if idle * T2 >= T3:  # t3: test the link when nothing has arrived for a while
                        writer.write(u_frame(TESTFR_ACT))
                        await writer.drain()
                        idle = 0
                    continue
                idle = 0
                c1 = apdu[0]
                if c1 == STARTDT_CON:
                    await link.send_i(interrogation(interrogate_ca))
                elif c1 == TESTFR_ACT:
                    writer.write(u_frame(TESTFR_CON))
                    await writer.drain()
                elif c1 & 0x01 == 0:
                    await link.received_i()
                    await on_asdu(decode_asdu(apdu[4:]))
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
        await asyncio.sleep(retry_s)
