"""IEC 104 framing and a full loop: simulated RTU to client over TCP."""

import asyncio
from datetime import datetime, timezone

from gridfeed import iec104 as p


def test_cp56_round_trip():
    ts = datetime(2016, 6, 9, 12, 45, 30, 250000, tzinfo=timezone.utc)
    assert p.parse_cp56(p.cp56(ts)) == ts


def test_i_frame_sequence_numbers():
    f = p.i_frame(300, 5, b"\x00")
    assert f[0] == 0x68 and f[1] == 5
    assert ((f[3] << 7) | (f[2] >> 1)) == 300
    assert ((f[5] << 7) | (f[4] >> 1)) == 5


def test_measured_value_with_quality_and_time():
    ts = datetime(2016, 6, 9, 0, 15, tzinfo=timezone.utc)
    asdu = p.measured_tf(6, [(1003, 408.5, 0x00, ts), (1004, 21.4, 0x80 | 0x40, ts)])
    a = p.decode_asdu(asdu)
    assert a.type_id == p.M_ME_TF_1 and a.ca == 6 and a.cot == p.COT_SPONT
    assert [x.ioa for x in a.points] == [1003, 1004]
    assert abs(a.points[0].value - 408.5) < 1e-4 and a.points[0].flags == []
    assert a.points[1].flags == ["NT", "IV"] and a.points[1].ts == ts


def test_rtu_to_client_over_tcp():
    async def run():
        rtu = p.RTU()
        server = await rtu.serve("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        got, stop = [], asyncio.Event()

        async def on_asdu(a):
            got.append(a)
            if sum(len(x.points) for x in got if x.type_id == p.M_ME_TF_1) >= 8:
                stop.set()

        await rtu.publish(5, [(1003, 409.0, 0, datetime(2016, 6, 9, tzinfo=timezone.utc))])
        task = asyncio.create_task(p.client("127.0.0.1", port, on_asdu, stop=stop, retry_s=0.1))
        for _ in range(50):
            if rtu.links:
                break
            await asyncio.sleep(0.02)
        for k in range(8):
            ts = datetime(2016, 6, 9, 0, 15 * (k + 1) % 60, tzinfo=timezone.utc)
            await rtu.publish(5, [(1003, 409.0 + k, 0, ts)])
        await asyncio.wait_for(stop.wait(), 5)
        task.cancel()
        server.close()
        return got

    got = asyncio.run(run())
    kinds = [a.type_id for a in got]
    assert p.C_IC_NA_1 in kinds  # interrogation confirmed and terminated
    gi = [a for a in got if a.type_id == p.M_ME_NC_1]
    assert gi and gi[0].points[0].value == 409.0  # interrogation returned the latest value
    spont = [pt.value for a in got if a.type_id == p.M_ME_TF_1 for pt in a.points]
    assert spont[:8] == [409.0 + k for k in range(8)]
