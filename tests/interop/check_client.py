"""Connect gridfeed's IEC 104 client to the c104 reference server and print what arrives."""
import asyncio
import sys

from gridfeed import iec104


async def main(host: str, port: int) -> None:
    got, stop = [], asyncio.Event()

    async def on_asdu(a):
        got.append(a)
        if sum(len(x.points) for x in got if x.type_id == iec104.M_ME_TF_1) >= 8:
            stop.set()

    task = asyncio.create_task(iec104.client(host, port, on_asdu, interrogate_ca=5, stop=stop, retry_s=0.5))
    await asyncio.wait_for(stop.wait(), 30)
    task.cancel()
    for a in got:
        for p in a.points:
            print(f"type {a.type_id:3} cot {a.cot:2} ca {a.ca} ioa {p.ioa} value {p.value:.2f} flags {p.flags} ts {p.ts}")
    print("types seen:", sorted({a.type_id for a in got}))


asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1", int(sys.argv[2]) if len(sys.argv) > 2 else 2405))
