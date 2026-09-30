"""Batch path: historical exports from a customer's system, through the same checks.

    python -m gridfeed.backfill export data/exports/week_v1.csv --swap "ONS Rural 1" "ONS Urban 6"
    python -m gridfeed.backfill import data/exports/week_v1.csv
    python -m gridfeed.backfill export data/exports/week_v2.csv      # the customer fixes the export
    python -m gridfeed.backfill import data/exports/week_v2.csv      # the swap issue closes itself

`export` writes the history window in the format a German metering system
might produce (semicolons, decimal commas, plain-text station names), with
optional mistakes. `import` maps, checks and stores it, records the run, and
closes issues from earlier imports that this file no longer reproduces.
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path

from . import store
from .grid import ROOT, load_model
from .mapping import load_all, load_devices
from .pipeline import Pipeline

LABEL = {
    "rural1": "ONS Rural 1", "rural2": "ONS Rural 2", "rural3": "ONS Rural 3",
    "semiurb4": "ONS Semiurban 4", "semiurb5": "ONS Semiurban 5", "urban6": "ONS Urban 6",
}
COLUMNS = ["Zeitstempel", "Station", "P [kW]", "Q [kvar]", "U [V]", "I [A]"]


def de(x: float, nd: int) -> str:
    return f"{x:.{nd}f}".replace(".", ",")


def export(path: Path, swap: tuple[str, str] | None, duplicate_day: bool) -> None:
    rows = []
    for station, label in LABEL.items():
        if swap and label in swap:
            label = swap[1] if label == swap[0] else swap[0]
        for r in csv.DictReader(open(ROOT / "data" / "profiles" / f"{station}_history.csv")):
            rows.append([r["ts"], label, de(float(r["p_w"]) / 1000, 3), de(float(r["q_var"]) / 1000, 3), de(float(r["u_v"]), 2), de(float(r["i_a"]), 2)])
    if duplicate_day:
        rows += [r for r in rows if r[1] == LABEL["rural2"] and r[0].startswith("2016-06-05")]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(COLUMNS)
        w.writerows(rows)
    print(f"wrote {len(rows)} rows to {path}" + (f" with {swap[0]} and {swap[1]} swapped" if swap else ""))


def run_import(path: Path) -> None:
    conn = store.connect()
    store.init(conn)
    mapping = load_all()["batch-export"]
    pipe = Pipeline(conn, load_model(), load_devices(), live=False)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO import_run (integration, source) VALUES (%s, %s) RETURNING id", (mapping.integration, path.name))
        run_id = cur.fetchone()[0]
    groups: dict[str, list] = defaultdict(list)
    with open(path, newline="") as f:
        reader = csv.DictReader(f, delimiter=mapping.delimiter)
        rows_in = 0
        for raw in reader:
            rows_in += 1
            device, ms, unmapped = mapping.parse(raw)
            groups[device].append((ms, unmapped))
    for device, items in groups.items():  # one device at a time, in file order
        for ms, unmapped in items:
            pipe.process(device, mapping.integration, ms, unmapped)
    pipe.issues.close_unseen(mapping.integration, set(groups), run_id)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE import_run SET rows_in=%s, values_stored=%s, issues_opened=%s, issues_closed=%s WHERE id=%s",
            (rows_in, pipe.stored, pipe.issues.opened, pipe.issues.closed, run_id),
        )
    print(f"import #{run_id} {path.name}: {rows_in} rows, {pipe.stored} values stored, "
          f"{pipe.issues.opened} issues opened, {pipe.issues.closed} closed")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("path", type=Path)
    e.add_argument("--swap", nargs=2, metavar=("A", "B"))
    e.add_argument("--duplicate-day", action="store_true")
    i = sub.add_parser("import")
    i.add_argument("path", type=Path)
    a = ap.parse_args()
    if a.cmd == "export":
        export(a.path, tuple(a.swap) if a.swap else None, a.duplicate_day)
    else:
        run_import(a.path)
