"""Generate what substation sensors would measure, by load flow over real profiles.

    python -m gridfeed.generate            # writes model/assets.json and data/profiles/*.csv

For each SimBench LV grid, every load and PV unit gets its real 15-minute
profile, a load flow runs at every step, and the transformer's low-voltage side
is read out: active and reactive power, busbar voltage and current. Two windows
are written: a "live" week in June (what the simulator streams) and the week
before it (a historical export for the batch backfill).
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .grid import GRIDS, ROOT, build_model

OUT = ROOT / "data" / "profiles"
YEAR_START = datetime(2016, 1, 1, tzinfo=timezone.utc)  # SimBench profiles cover 2016
STEP = timedelta(minutes=15)
WINDOWS = {"history": (160 - 7, 7), "live": (160, 3)}  # (first day of year, days)


def run(grid: str, code: str) -> dict[str, list[dict]]:
    import pandapower as pp
    import simbench as sb

    net = sb.get_simbench_net(code)
    prof = sb.get_absolute_values(net, profiles_instead_of_study_cases=True)
    trafo = net.trafo.index[0]
    # The 2034 scenario includes home batteries whose profile sign convention is
    # not clear from the data, so they are left out rather than guessed.
    net.storage["in_service"] = False
    out: dict[str, list[dict]] = {}
    for name, (day, days) in WINDOWS.items():
        rows = []
        for step in range(day * 96, (day + days) * 96):
            net.load["p_mw"] = prof[("load", "p_mw")].iloc[step].values
            net.load["q_mvar"] = prof[("load", "q_mvar")].iloc[step].values
            net.sgen["p_mw"] = prof[("sgen", "p_mw")].iloc[step].values
            pp.runpp(net, numba=False)
            r = net.res_trafo.loc[trafo]
            rows.append(
                {
                    "ts": (YEAR_START + step * STEP).isoformat(),
                    # Measured at the LV side, positive when power flows into the LV grid.
                    "p_w": round(-r.p_lv_mw * 1e6, 1),
                    "q_var": round(-r.q_lv_mvar * 1e6, 1),
                    "u_v": round(r.vm_lv_pu * net.trafo.vn_lv_kv.iloc[0] * 1000, 2),
                    "i_a": round(r.i_lv_ka * 1000, 2),
                }
            )
        out[name] = rows
        print(f"  {grid:9} {name:7} {len(rows)} steps, peak {max(abs(x['p_w']) for x in rows) / 1000:.1f} kW")
    return out


def main() -> None:
    model = build_model()
    print(f"model: {len(model)} transformers")
    OUT.mkdir(parents=True, exist_ok=True)
    for grid, code in GRIDS.items():
        for name, rows in run(grid, code).items():
            with open(OUT / f"{grid}_{name}.csv", "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)


if __name__ == "__main__":
    main()
