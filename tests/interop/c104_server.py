"""Reference IEC 104 server built with c104 (Fraunhofer FIT, GPLv3), run in Docker.

Used only to check that gridfeed's own client understands an independent
implementation. It publishes station 5 with the same information object
addresses as the demo RTU.
"""
import time

import c104

server = c104.Server(ip="0.0.0.0", port=2404)
station = server.add_station(common_address=5)
points = {
    1001: station.add_point(io_address=1001, type=c104.Type.M_ME_TF_1),
    1002: station.add_point(io_address=1002, type=c104.Type.M_ME_TF_1),
    1003: station.add_point(io_address=1003, type=c104.Type.M_ME_TF_1),
    1004: station.add_point(io_address=1004, type=c104.Type.M_ME_TF_1),
}
server.start()
values = {1001: 12.72, 1002: 8.31, 1003: 408.78, 1004: 21.46}
k = 0
while True:
    for addr, point in points.items():
        point.value = values[addr] + (0.01 * k if addr == 1003 else 0)
    for point in points.values():
        point.transmit(cause=c104.Cot.SPONTANEOUS)
    k += 1
    time.sleep(1)
