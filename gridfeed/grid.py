"""The grid model: six real German low-voltage grids from SimBench.

Each SimBench LV grid is fed by one local substation (Ortsnetzstation) with a
20/0.4 kV transformer. The model here is what an integration checks
measurements against: asset ids, ratings and transformer impedance. The
generator runs a load flow over real SimBench load and PV profiles to produce
what sensors in those substations would measure.

SimBench data: simbench.de, CC BY 4.0.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL = ROOT / "model" / "assets.json"

GRIDS = {
    "rural1": "1-LV-rural1--2-sw",
    "rural2": "1-LV-rural2--0-sw",
    "rural3": "1-LV-rural3--0-sw",
    "semiurb4": "1-LV-semiurb4--0-sw",
    "semiurb5": "1-LV-semiurb5--0-sw",
    "urban6": "1-LV-urban6--0-sw",
}


@dataclass(frozen=True)
class Transformer:
    asset_id: str  # the SimBench transformer name, as a grid operator's GIS would hold it
    grid: str
    sn_kva: float
    vn_lv_v: float
    vk_percent: float
    vkr_percent: float
    mv_setpoint_pu: float  # voltage the upstream grid holds at the MV side

    def expected_lv_voltage(self, p_w: float, q_var: float) -> float:
        """Busbar voltage the model predicts for a measured P and Q.

        Standard approximation of the voltage drop across a transformer:
        du = r*p + x*q in per unit of rating. Good to a fraction of a percent at
        these loadings, which is what the model-consistency check needs.
        """
        sn = self.sn_kva * 1000
        r = self.vkr_percent / 100
        x = math.sqrt(max(self.vk_percent**2 - self.vkr_percent**2, 0.0)) / 100
        du = r * p_w / sn + x * q_var / sn
        return self.vn_lv_v * self.mv_setpoint_pu * (1 - du)


def load_model(path: Path = MODEL) -> dict[str, Transformer]:
    return {a["asset_id"]: Transformer(**a) for a in json.loads(path.read_text())}


def build_model() -> dict[str, Transformer]:
    import simbench as sb

    out = {}
    for grid, code in GRIDS.items():
        net = sb.get_simbench_net(code)
        t = net.trafo.iloc[0]
        tr = Transformer(
            asset_id=str(t["name"]),
            grid=grid,
            sn_kva=float(t.sn_mva) * 1000,
            vn_lv_v=float(t.vn_lv_kv) * 1000,
            vk_percent=float(t.vk_percent),
            vkr_percent=float(t.vkr_percent),
            mv_setpoint_pu=float(net.ext_grid.vm_pu.iloc[0]),
        )
        out[tr.asset_id] = tr
    MODEL.parent.mkdir(parents=True, exist_ok=True)
    MODEL.write_text(json.dumps([asdict(t) for t in out.values()], indent=1))
    return out
