"""Re-attach to the 2026-10-07 calibration run after a kernel restart and resume it with the 2026-10-08 options.

Run from the notebook, after the setup cells (devices, LI-6800, protocol) and NOT the "Run" cell:
    %run -i resume_calib_2026-10-07.py
Uses the notebook's `dots`, `licor` and `P`; leaves `run` and `status` in the notebook namespace.
"""
import dataclasses
import importlib
import json
from datetime import datetime
from pathlib import Path

import co2dot_calib
importlib.reload(co2dot_calib)
from co2dot_calib import Run, Protocol

for name in ("dots", "licor", "P"):
    if name not in globals():
        raise NameError(f"`{name}` is not defined: run the notebook's setup cells first (devices, LI-6800, protocol)")

OUT = Path("co2dot_data") / "2026-10-07_18-11-54_calib"
cp = json.loads((OUT / "checkpoint.json").read_text(encoding="utf-8"))
if cp.get("status") == "running":
    raise RuntimeError(f"checkpoint says the run is still running ({cp}); interrupt it first")

changes = dict(co2_zero_tol_ppm=15.0, settle_at_next_co2=True, cell_zero_dwell_s=180.0,
               rh_settle_min_s=20 * 60, t_settle_min_s=10 * 60, guard_wait_s=600.0,
               cooldown_dry=True, max_dew_c=20.0)
P2 = Protocol(**{**dataclasses.asdict(P), **changes})

# the rebuilt step list must match every step the original run already logged
st = P2.steps()
ev = [json.loads(l) for l in (OUT / "events.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
done = [e for e in ev if e["kind"] == "step_done"]
bad = [e["index"] for e in done if (st[e["index"]]["co2"], st[e["index"]]["t"], st[e["index"]]["rh"]) != (e["co2"], e["t"], e["rh"])]
assert not bad, f"step list differs from the original run at steps {bad[:10]}"

meta_orig = json.loads((OUT / "meta.json").read_text(encoding="utf-8"))
run = Run(OUT, dots, licor, P2, cadence_s=10.0, led_ma=10)          # rewrites meta.json ...
start = cp["next_index"]
meta = json.loads((OUT / "meta.json").read_text(encoding="utf-8"))
meta["protocol"] = meta_orig["protocol"]                             # ... so keep the original protocol
meta["protocol_changes"] = meta_orig.get("protocol_changes", []) + [
    {"from_index": start, "ts": datetime.now().isoformat(timespec="seconds"), "changes": changes}]
(OUT / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
with (OUT / "cycles.jsonl").open("rb") as f:
    run.cycle = json.loads(f.readlines()[-1])["cycle"] + 1           # keep cycle ids unique

print(f"{len(done)} logged steps match; resuming {OUT.name} at step {start}, cycle {run.cycle}")
run.event("protocol_changed", from_index=start, changes=changes)
status = run.run(start_index=start)
print("status:", status)
