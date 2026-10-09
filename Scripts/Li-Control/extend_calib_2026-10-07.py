"""Continue the 2026-10-07 calibration run after pass 2 with the extra RH 75 % block (22 C, then 17 C).

Queue it in the notebook right after the running cell (it waits until pass 2 returns 'done'):
    %run -i extend_calib_2026-10-07.py
Does nothing unless the checkpoint says pass 2 finished ('done' at the last pass-2 step), so an interrupt or a
cartridge pause is never resumed by accident. After the extra block the logger is restarted and keeps logging
(phase 'post_run') for the optional saturation exposure:
    saturation("start", source="breath bag")   ...10 min...   saturation("end")
then the notebook's Close cell (run.close()) stops it.
"""
import dataclasses
import importlib
import json
from datetime import datetime
from pathlib import Path

import co2dot_calib
importlib.reload(co2dot_calib)
from co2dot_calib import Run, Protocol

OUT = Path(globals().get("CALIB_OUT", Path("co2dot_data") / "2026-10-07_18-11-54_calib"))
EXTRA = globals().get("CALIB_EXTRA", dict(extra_cells=((22, 75), (17, 75)), extra_dwell_s=600.0))


def saturation(action: str, **info) -> None:
    """Mark the start/end of the optional >2 % CO2 exposure (chamber at the anchor condition, logger running)."""
    run.set_phase(kind="saturation" if action == "start" else "post_run", step=None)
    run.event("saturation", action=action, **info)


if Path(run.outdir).resolve() != OUT.resolve():
    raise RuntimeError(f"`run` points to {run.outdir}, expected {OUT}")
cp = json.loads((OUT / "checkpoint.json").read_text(encoding="utf-8"))
old = run.protocol.steps()
P3 = Protocol(**{**dataclasses.asdict(run.protocol), **EXTRA})
new = P3.steps()
assert new[:len(old)] == old, "the existing steps must not change"

if cp.get("status") != "done" or cp.get("next_index") != len(old):
    print(f"not continuing: checkpoint is {cp}, expected 'done' at step {len(old)} (the end of pass 2)")
else:
    meta = json.loads((OUT / "meta.json").read_text(encoding="utf-8"))
    meta.setdefault("protocol_changes", []).append(
        {"from_index": len(old), "ts": datetime.now().isoformat(timespec="seconds"), "changes": EXTRA,
         "appended_steps": [{k: s.get(k) for k in ("index", "kind", "t", "rh", "co2", "dwell_s")} for s in new[len(old):]]})
    (OUT / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    run.__class__ = Run
    run.protocol = P = P3
    for s in new[len(old):]:
        print(f"  step {s['index']:3d} {s['kind']:12s} T={s['t']} RH={s['rh']} CO2={s['co2']} dwell={s['dwell_s']}")
    run.event("protocol_changed", from_index=len(old), changes=EXTRA)
    status = run.run(start_index=len(old))
    print("status:", status)
    if status == "done":
        run.set_phase(kind="post_run", step=None)
        run.start_logger()                      # keep logging at the anchor for the optional saturation exposure
        print('logger running (phase post_run): saturation("start", source=...) / saturation("end"), then run.close()')
