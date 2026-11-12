"""
co2dot_calib3.py -- CO2Dot calibration protocol 3: a CAMPAIGN of sessions of at most `session_budget_h` hours.

Why a third protocol
  * Protocol 1 (co2dot_calib.py) measured the gen1 film at 10 mA every 10 s for 50 h: the LED dose consumed base
    and alpha drifted +35 %. Protocol 2 (co2dot_calib2.py) cut the dose (4 mA, 60 s windows) but has no anchors,
    a fixed grid repeated whole, and no record of the step kinetics.
  * Protocol 3 keeps protocol 2's dose discipline and adds what the model fit needs: a campaign ledger so every
    (RH, T) cell is measured `repeats` times on different days and in different orders, anchor + zero bookends in
    every session (the only ruler for film ageing between sessions), an adaptive dwell that ends when the signal
    is flat, 15 s logging during the approach (kinetics) and 5 s in the 60 s window, and spectrometer settings
    frozen for the whole campaign.

Files
  <campaign>/campaign.json              master grid, repeats, frozen settings, ledger (cell -> count, sessions)
  <campaign>/<timestamp>_session/       one run folder per session: cycles.jsonl, events.jsonl, meta.json,
                                        session_plan.json (planned cells + done flags, for resume)
  load_run() of co2dot_calib.py reads a session folder; the analysis notebook concatenates all sessions of a
  campaign folder automatically (phase tags: kind anchor/zero/co2/settle, step, cell, repeat as pass_, transition,
  anchor start/end). A session ends on its budget, on the STOP file, on the CO2 or humidity watchdog, or on Ctrl-C;
  in every case but Ctrl-C the end anchor pair is measured before the chamber is restored (end_anchor_* options).

Use (see CO2Dot_calibRun3.ipynb)
    camp = Campaign(folder, Protocol3(), repeats=3)          # creates or opens campaign.json
    dots = connect_dots(labels={...}); camp.freeze_settings(dots, led_ma=4, autotune_target=30000)  # first session only
    run  = Session(camp, dots, LicorLive(li)); run.run()      # <= 7 h, Ctrl-C safe, run() again resumes
    camp.status()

Dry run: lic = MockLicor(tau_s={"co2": 2, "t": 2, "rh": 2}); camp = Campaign(tmp, Protocol3.quick(), repeats=2)
         Session(camp, mock_dots(2, lic), lic).run()
"""
from __future__ import annotations

import json
import math
import random
import statistics
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from co2dot_calib import (CHANNELS, AS7341_FULL_SCALE, DotHandle, Protocol, Run, MockLicor, mock_dots,
                          dew_point_c, apply_settings, preflight, _json_default)

LED_MA_MIN = 4


# ----------------------------------------------------------------------------- #
# protocol
# ----------------------------------------------------------------------------- #
@dataclass
class Protocol3(Protocol):
    """Grid and timing of a campaign. Inherits the LI-6800 tolerances and guards of Protocol (co2dot_calib.py)."""
    rh_levels: Tuple[float, ...] = (20, 35, 50, 65, 80)
    t_levels: Tuple[float, ...] = (17, 22, 27, 32)
    co2_levels: Tuple[float, ...] = (0, 200, 400, 800, 2000, 0)      # the closing zero exposes drift inside the cell
    co2_jitter_ppm: float = 30.0                                      # on the middle levels only
    co2_zero_tol_ppm: Optional[float] = 15.0                          # scrub floor is 5-9 ppm
    co2_top_tol_ppm: float = 40.0                                     # the mixer overshoots and creeps at the top
    co2_tol_ppm: float = 3.0
    co2_timeout_s: float = 300.0
    max_dew_c: Optional[float] = 19.5                                 # humidifier ceiling (dew point of the target air)
    dewpoint_margin_c: float = 3.0
    cooldown_dry: bool = True
    rh_cap_by_t: Dict[float, float] = field(default_factory=dict)     # no static cap: the dew-point rules decide
    # dwell and logging
    approach_period_s: float = 15.0                                   # logger cadence while approaching / dwelling
    window_period_s: float = 5.0                                      # cadence inside the measurement window
    window_s: float = 60.0
    dwell_min_s: float = 60.0                                         # after the setpoint is reached, before the window
    dwell_min_humid_s: float = 180.0                                  # at rh >= humid_rh (slower film)
    humid_rh: float = 65.0
    dwell_max_s: float = 300.0
    flat_window_s: float = 60.0                                       # the window may start when |dy| over this < flat_tol
    flat_tol: float = 2e-4
    # cell settles (film hydration / temperature)
    t_settle_min_s: float = 600.0
    rh_settle_min_s: float = 1200.0
    rh_settle_max_s: float = 2700.0
    drift_window_s: float = 600.0
    drift_tol: float = 5e-4
    # anchors
    anchor: Dict[str, float] = field(default_factory=lambda: {"t": 25.0, "rh": 50.0, "co2": 400.0})
    # session end: stop file, watchdogs and the end anchor pair
    end_anchor_on_stop: bool = True          # stop file / watchdog / budget: measure the end anchor pair before standby
    end_anchor_on_interrupt: bool = False    # Ctrl-C: also measure it (a second Ctrl-C during the pair always aborts it)
    stop_file_name: str = "STOP"             # create this file in the session or the campaign folder to end the session cleanly
    co2_watchdog_strikes: int = 2            # consecutive CO2 timeouts far below a setpoint above ambient -> cartridge empty
    co2_watchdog_gap_ppm: float = 100.0      # "far below": setpoint minus CO2_s at the timeout
    rh_watchdog_strikes: int = 3             # consecutive RH timeouts -> desiccant or humidifier exhausted
    # campaign / session planning
    session_budget_h: float = 7.0
    max_rh_levels_per_session: int = 2
    est_cell_s: float = 35 * 60.0
    est_rh_change_s: float = 40 * 60.0
    est_t_change_s: float = 12 * 60.0
    est_anchor_s: float = 15 * 60.0
    flow_umol_s: float = 400.0

    @classmethod
    def quick(cls) -> "Protocol3":
        """Seconds instead of minutes, for mock dry runs."""
        return cls(rh_levels=(40, 60), t_levels=(20, 28), co2_levels=(0, 300, 2000, 0), max_dew_c=None,
                   co2_timeout_s=20, t_timeout_s=30, rh_timeout_s=30, approach_period_s=1.0, window_period_s=0.5,
                   window_s=4.0, dwell_min_s=3.0, dwell_min_humid_s=4.0, dwell_max_s=8.0, flat_window_s=3.0,
                   flat_tol=5e-3, t_settle_min_s=4.0, rh_settle_min_s=4.0, rh_settle_max_s=8.0, drift_window_s=4.0,
                   drift_tol=5e-3, session_budget_h=0.1, est_cell_s=40.0, est_rh_change_s=10.0, est_t_change_s=8.0,
                   est_anchor_s=15.0)

    def allowed(self, t: float, rh: float) -> Tuple[bool, str]:
        td = dew_point_c(t, rh)
        if self.max_dew_c is not None and td > self.max_dew_c:
            return False, f"dew point {td:.1f} C above the humidifier ceiling {self.max_dew_c} C"
        if t - td < self.dewpoint_margin_c:
            return False, f"dew-point margin {t - td:.1f} C < {self.dewpoint_margin_c} C"
        cap = self.rh_cap_by_t.get(t)
        if cap is not None and rh > cap:
            return False, f"rh {rh} > cap {cap} at {t} C"
        return True, ""

    def cells(self) -> List[Tuple[float, float]]:
        return [(rh, t) for rh in self.rh_levels for t in self.t_levels if self.allowed(t, rh)[0]]

    def dwell_min_for(self, rh: float) -> float:
        return self.dwell_min_humid_s if rh >= self.humid_rh else self.dwell_min_s

    def feasibility_table(self) -> str:
        lines = ["T(C) | " + " | ".join(f"RH {rh:>3.0f}%" for rh in self.rh_levels)]
        for t in self.t_levels:
            lines.append(f"{t:4.0f} | " + " | ".join(("  ok   " if self.allowed(t, rh)[0] else " skip  ") for rh in self.rh_levels))
        return "\n".join(lines)


# ----------------------------------------------------------------------------- #
# campaign ledger and session planner
# ----------------------------------------------------------------------------- #
def cell_key(rh: float, t: float) -> str:
    return f"{rh:g}/{t:g}"


class Campaign:
    """campaign.json: the master plan, frozen spectrometer settings and the ledger of what has been measured."""

    def __init__(self, folder, protocol: Optional[Protocol3] = None, repeats: int = 3):
        self.folder = Path(folder); self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / "campaign.json"
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            self.protocol = Protocol3(**self.data["protocol"]) if protocol is None else protocol
            if protocol is not None:
                self.data["protocol"] = asdict(protocol)
        else:
            self.protocol = protocol or Protocol3()
            self.data = {"created": datetime.now().isoformat(timespec="seconds"), "repeats": repeats,
                         "protocol": asdict(self.protocol), "led_ma": LED_MA_MIN, "settings": {},
                         "ledger": {}, "sessions": [], "cell_time_s": []}
        self.data.setdefault("repeats", repeats)
        self._save()

    def _save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2, default=_json_default), encoding="utf-8")

    # -- settings frozen for the whole campaign ---------------------------- #
    def freeze_settings(self, dots: Dict[str, DotHandle], led_ma: int = LED_MA_MIN,
                        autotune_target: Optional[float] = 30000.0, gain: Optional[int] = None,
                        atime: Optional[int] = None, astep: Optional[int] = None) -> Dict[str, Any]:
        """First session: auto-tune (co2dot_calib2.autotune) or set gain/atime/astep, then store them per device.
        Later sessions: apply_settings(dots) re-applies the stored values; never re-tune inside a campaign."""
        if self.data["settings"]:
            raise RuntimeError("settings are already frozen for this campaign; call apply_settings(dots) instead")
        self.data["led_ma"] = int(led_ma)
        if autotune_target is not None and gain is None:
            from co2dot_calib2 import autotune
            self.data["autotune"] = autotune(dots, led_ma=led_ma, target_counts=autotune_target)
        else:
            apply_settings(dots, gain=gain, atime=atime, astep=astep)
        for dev_id, h in dots.items():
            st = h.dot.spec_status()
            self.data["settings"][dev_id] = {k: st.get(k) for k in ("gain", "atime", "astep")}
        self._save()
        return self.data["settings"]

    def apply_settings(self, dots: Dict[str, DotHandle]) -> Dict[str, Any]:
        """Re-apply the frozen gain/atime/astep to every known device; warn about unknown devices."""
        out = {}
        for dev_id, h in dots.items():
            s = self.data["settings"].get(dev_id)
            if s is None:
                print(f"WARNING: {dev_id} ({h.label}) has no frozen settings in this campaign; freeze_settings() first")
                continue
            h.dot.set_spectrometer(**{k: int(v) for k, v in s.items() if v is not None})
            out[dev_id] = h.dot.spec_status()
        return out

    # -- ledger ------------------------------------------------------------- #
    def count(self, rh: float, t: float) -> int:
        return int(self.data["ledger"].get(cell_key(rh, t), {}).get("count", 0))

    def record_cell(self, rh: float, t: float, session: str, seconds: float, ok: bool) -> None:
        e = self.data["ledger"].setdefault(cell_key(rh, t), {"count": 0, "sessions": []})
        e["sessions"].append({"session": session, "ts": datetime.now().isoformat(timespec="seconds"),
                              "seconds": round(seconds), "ok": ok})
        if ok:
            e["count"] += 1
        self.data["cell_time_s"] = (self.data.get("cell_time_s", []) + [round(seconds)])[-30:]
        self._save()

    def record_session(self, summary: Dict[str, Any]) -> None:
        self.data["sessions"].append(summary); self._save()

    def est_cell_s(self) -> float:
        hist = self.data.get("cell_time_s", [])
        return float(statistics.median(hist)) if len(hist) >= 3 else self.protocol.est_cell_s

    def remaining(self) -> List[Tuple[float, float]]:
        return [(rh, t) for rh, t in self.protocol.cells() if self.count(rh, t) < self.data["repeats"]]

    def plan_session(self, seed: Optional[int] = None) -> Tuple[List[Tuple[float, float]], float]:
        """Cells for the next session: least-measured first, at most `max_rh_levels_per_session` RH levels,
        RH ascending (dry before humid), T order shuffled, cut to the time budget. Returns (cells, estimate_s)."""
        P = self.protocol
        rng = random.Random(seed if seed is not None else len(self.data["sessions"]) + 1000 * P.seed)
        need = self.remaining()
        if not need:
            return [], 0.0
        rh_rank = {}
        for rh in sorted({rh for rh, _ in need}):
            rh_rank[rh] = (min(self.count(r, t) for r, t in need if r == rh), rng.random())
        chosen = sorted(sorted(rh_rank, key=lambda r: rh_rank[r])[:P.max_rh_levels_per_session])
        budget = P.session_budget_h * 3600 - 2 * P.est_anchor_s
        est, plan = 0.0, []
        cell_s = self.est_cell_s()
        for rh in chosen:
            ts = [t for r, t in need if r == rh]
            ts.sort(key=lambda t: (self.count(rh, t), rng.random()))
            first = True
            for t in ts:
                cost = cell_s + (P.est_rh_change_s if first else P.est_t_change_s)
                if est + cost > budget:
                    break
                plan.append((rh, t)); est += cost; first = False
        return plan, est + 2 * P.est_anchor_s

    def status(self) -> str:
        P = self.protocol
        lines = [f"campaign {self.folder.name}: {len(self.data['sessions'])} session(s), repeats wanted {self.data['repeats']}, "
                 f"frozen settings for {len(self.data['settings'])} device(s), LED {self.data['led_ma']} mA"]
        lines.append("T(C) | " + " | ".join(f"RH {rh:>3.0f}%" for rh in P.rh_levels))
        for t in P.t_levels:
            cells = []
            for rh in P.rh_levels:
                cells.append(f"  {self.count(rh, t)}/{self.data['repeats']}  " if P.allowed(t, rh)[0] else "  skip  ")
            lines.append(f"{t:4.0f} | " + " | ".join(cells))
        if self.data["sessions"]:
            s = self.data["sessions"][-1]
            lines.append(f"last session {s.get('folder')}: {s.get('status')} ({s.get('end_reason') or 'n/a'}), "
                         f"end anchor {'yes' if s.get('end_anchor') else 'no'}")
        plan, est = self.plan_session()
        lines.append(f"next session: {len(plan)} cell(s) {plan}, estimated {est/3600:.1f} h" if plan else "campaign complete")
        return "\n".join(lines)


# ----------------------------------------------------------------------------- #
# session
# ----------------------------------------------------------------------------- #
class SessionStop(Exception):
    """Ends a session cleanly from inside a step (STOP file or watchdog); the end anchor pair follows."""

    def __init__(self, reason: str, **detail: Any):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


class Session(Run):
    """One session of at most session_budget_h: anchor+zero, planned cells, anchor+zero. Ctrl-C safe, resumable."""

    def __init__(self, campaign: Campaign, dots: Dict[str, DotHandle], licor, *, outdir=None, seed: Optional[int] = None,
                 meta_extra: Optional[Dict[str, Any]] = None, verbose: bool = True, publisher=None):
        P = campaign.protocol
        outdir = Path(outdir) if outdir else campaign.folder / (datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + "_session")
        super().__init__(outdir, dots, licor, P, cadence_s=P.approach_period_s, led_ma=int(campaign.data["led_ma"]),
                         meta_extra={"protocol_version": 3, "campaign": str(campaign.path),
                                     "frozen_settings": campaign.data["settings"], **(meta_extra or {})}, verbose=verbose)
        self.campaign, self.P = campaign, P
        self.plan_path = self.outdir / "session_plan.json"
        if self.plan_path.exists():
            self.plan = json.loads(self.plan_path.read_text(encoding="utf-8"))
        else:
            cells, est = campaign.plan_session(seed)
            self.plan = {"cells": [list(c) for c in cells], "done": [], "estimate_s": est, "seed": seed,
                         "started": datetime.now().isoformat(timespec="seconds")}
            self._save_plan()
        self.publisher = publisher                       # openjii_stream.OpenJIIPublisher or None
        if publisher is not None:
            self.on_cycle = publisher.publish_cycle      # every logged cycle also goes to openJII (non-blocking)
            self.event("openjii_streaming", topic=getattr(publisher, "topic", None))
        self.t_session0 = time.monotonic()
        self.anchor_results: List[Dict[str, Any]] = []
        self.flashes = 0
        self.end_reason: Optional[str] = None            # complete | budget | stop_file | co2_watchdog | rh_watchdog | interrupt
        self._anchor_tag: Optional[str] = None           # "start" / "end" while an anchor pair is being measured
        self._in_end_anchor = False                      # watchdogs and the STOP file are ignored during the end anchor
        self._co2_strikes = 0
        self._rh_strikes = 0
        self.stop_files = [self.outdir / P.stop_file_name, campaign.folder / P.stop_file_name]

    def _save_plan(self) -> None:
        self.plan_path.write_text(json.dumps(self.plan, indent=2), encoding="utf-8")

    # -- measurement primitives --------------------------------------------- #
    def _flat(self, window_s: float, tol: float) -> Optional[bool]:
        """True when log10(630/515) changed less than tol over the last window_s on every device."""
        now = time.monotonic()
        pts = [c for c in list(self.recent) if now - c["t_mono"] <= window_s]
        if len(pts) < 4:
            return None
        worst = 0.0
        for d in self.dots:
            xs, ys = [], []
            for c in pts:
                r = c["devices"].get(d, {}).get("spec_flash")
                if r and r["diff"].get("f7_630", 0) > 0 and r["diff"].get("f4_515", 0) > 0:
                    xs.append(c["t_mono"]); ys.append(math.log10(r["diff"]["f7_630"] / r["diff"]["f4_515"]))
            if len(xs) < 4:
                return None
            mx, my = statistics.mean(xs), statistics.mean(ys)
            sxx = sum((x - mx) ** 2 for x in xs)
            slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx else 0.0
            worst = max(worst, abs(slope * window_s))
        return worst < tol

    def _set_cadence(self, period_s: float) -> None:
        self.cadence_s = period_s

    # -- clean stop ----------------------------------------------------------- #
    def request_stop(self) -> Path:
        """Ask a running session to end after the current step; the end anchor pair follows. From another process
        or kernel: create the file STOP (P.stop_file_name) in the session folder or in the campaign folder."""
        p = self.stop_files[0]
        p.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
        return p

    def _check_stop(self) -> None:
        if self._in_end_anchor:
            return
        for p in self.stop_files:
            if p.exists():
                try:
                    p.unlink()                      # consumed, so that the next session does not stop at once
                except OSError:
                    pass
                self.event("stop_requested", file=str(p))
                raise SessionStop("stop_file", file=str(p))

    def _end_anchor(self, reason: str) -> bool:
        """The end anchor pair after any kind of session end; Ctrl-C during it aborts it and is logged."""
        self._in_end_anchor = True
        try:
            self._anchor_pair("end", reason=reason)
            return True
        except KeyboardInterrupt:
            self.status = "interrupted"
            self.event("end_anchor_aborted", reason=reason)
            return False
        except Exception as e:                       # e.g. LI-6800 link lost: the session still ends cleanly
            self.event("end_anchor_failed", reason=reason, error=str(e)[:300])
            return False
        finally:
            self._in_end_anchor = False

    def _co2_step(self, cell: Dict[str, Any], sp: float, nominal: float, kind: str) -> Dict[str, Any]:
        """Setpoint -> wait within tolerance -> adaptive dwell -> 60 s window at window_period_s."""
        P = self.P
        self._check_stop()
        base = dict(kind=kind, step=self.step_index, cell=cell_key(cell["rh"], cell["t"]), rh=cell["rh"], t=cell["t"],
                    pass_=cell["repeat"], co2=sp, co2_nominal=nominal, session=self.outdir.name)
        if kind in ("anchor", "zero") and self._anchor_tag:
            base["anchor"] = self._anchor_tag
        self.set_phase(**base, transition=True)
        self.licor.set(co2_s=sp)
        tol = P.co2_zero_tol_ppm if nominal == 0 else (P.co2_top_tol_ppm if nominal == max(P.co2_levels) else max(P.co2_tol_ppm, 0.01 * sp))
        ok, dt = self._wait("CO2_s", sp, tol, P.co2_timeout_s)
        co2_now = self.licor.get("CO2_s")
        self.event("co2_reached" if ok else "co2_timeout", step=self.step_index, step_kind=kind, co2=sp, after_s=round(dt),
                   CO2_s=co2_now)
        if ok:
            self._co2_strikes = 0
        elif (not self._in_end_anchor and sp > P.ambient_restore_ppm
              and isinstance(co2_now, (int, float)) and sp - co2_now > P.co2_watchdog_gap_ppm):
            self._co2_strikes += 1                  # a setpoint above ambient not reached and CO2_s far below it
            self.event("cartridge_suspected", step=self.step_index, co2=sp, CO2_s=co2_now, strike=self._co2_strikes)
            if self._co2_strikes >= P.co2_watchdog_strikes:
                raise SessionStop("co2_watchdog", co2=sp, CO2_s=co2_now, strikes=self._co2_strikes)
        t0 = time.monotonic(); dmin = P.dwell_min_for(cell["rh"]); flat = None
        while True:
            self._check_stop()
            el = time.monotonic() - t0
            flat = self._flat(P.flat_window_s, P.flat_tol)
            if el >= dmin and (flat or el >= P.dwell_max_s):
                break
            self._hold(min(10.0, max(P.approach_period_s, 0.5)))
        dwell = time.monotonic() - t0
        self.set_phase(**base, transition=False)
        self._set_cadence(P.window_period_s)
        self._hold(P.window_s)
        self._set_cadence(P.approach_period_s)
        rec = dict(step=self.step_index, step_kind=kind, co2=sp, co2_nominal=nominal, reached=ok, dwell_s=round(dwell), flat=flat,
                   CO2_s=self.licor.get("CO2_s"), Tchamber=self.licor.get("Tchamber"), RHcham=self.licor.get("RHcham"))
        self.event("step_done", **rec)
        self.step_index += 1
        return rec

    def _goto(self, t: float, rh: float, co2: float, new_rh: bool) -> Tuple[bool, bool]:
        """Move the chamber to (t, rh) with co2 already set; dry before cooling; wait for T and RH; hold for the film."""
        P = self.P
        cur_t = self.licor.get("Tchamber")
        self.licor.set(co2_s=co2)
        if P.cooldown_dry and isinstance(cur_t, (int, float)) and t < cur_t - 0.5:
            self._dry_before_cooling(self.step_index, t)
            self.licor.set(tair=t)
            ok_t, dt_t = self._wait("Tchamber", t, P.t_tol_c, P.t_timeout_s)
            self.licor.set(rh_air=rh)
        else:
            self.licor.set(tair=t); self.licor.set(rh_air=rh)
            ok_t, dt_t = self._wait("Tchamber", t, P.t_tol_c, P.t_timeout_s)
        self.event("t_reached" if ok_t else "t_timeout", step=self.step_index, t=t, after_s=round(dt_t), Tchamber=self.licor.get("Tchamber"))
        ok_rh, dt_rh = self._wait("RHcham", rh, P.rh_tol_pct, P.rh_timeout_s)
        rh_now = self.licor.get("RHcham")
        self.event("rh_reached" if ok_rh else "rh_timeout", step=self.step_index, rh=rh, after_s=round(dt_rh), RHcham=rh_now)
        if ok_rh:
            self._rh_strikes = 0
        elif not self._in_end_anchor:
            self._rh_strikes += 1                   # desiccant or humidifier at its floor / ceiling
            self.event("humidity_suspected", step=self.step_index, rh=rh, RHcham=rh_now, strike=self._rh_strikes)
            if self._rh_strikes >= P.rh_watchdog_strikes:
                raise SessionStop("rh_watchdog", rh=rh, RHcham=rh_now, strikes=self._rh_strikes)
        guard = self._dewpoint_guard(t)
        if guard:
            self.event("condensation_guard", step=self.step_index, detail=guard)
            return False, False
        t0 = time.monotonic(); min_hold = P.rh_settle_min_s if new_rh else P.t_settle_min_s
        max_hold = P.rh_settle_max_s if new_rh else max(P.t_settle_min_s, P.rh_settle_min_s / 2)
        drift = None
        while True:
            self._check_stop()
            el = time.monotonic() - t0
            drift = self.film_drift()
            if el >= min_hold and ((drift is not None and drift < P.drift_tol) or el >= max_hold):
                break
            self._hold(min(30.0, max(P.drift_window_s / 4, 1.0)))
        self.event("cell_settled", step=self.step_index, t=t, rh=rh, held_s=round(time.monotonic() - t0),
                   drift=None if drift is None else round(drift, 5), new_rh=new_rh)
        return True, (ok_t and ok_rh)

    def _anchor_pair(self, tag: str, reason: Optional[str] = None) -> None:
        a = self.P.anchor; cell = dict(rh=a["rh"], t=a["t"], repeat=len(self.campaign.data["sessions"]))
        self._anchor_tag = tag
        try:
            self.set_phase(kind="settle", step=self.step_index, rh=a["rh"], t=a["t"], co2=None, pass_=cell["repeat"],
                           session=self.outdir.name, anchor=tag)
            self.event("anchor_pair_started", tag=tag, reason=reason)
            ok, _ = self._goto(a["t"], a["rh"], a["co2"], new_rh=True)
            r1 = self._co2_step(cell, a["co2"], a["co2"], "anchor")
            r2 = self._co2_step(cell, 0.0, 0.0, "zero")
            self.anchor_results.append({"tag": tag, "reason": reason, "anchor": r1, "zero": r2})
            self.event("anchor_pair_done", tag=tag, reason=reason)
        finally:
            self._anchor_tag = None

    def _measure_cell(self, rh: float, t: float, first_in_block: bool) -> bool:
        P = self.P; rng = random.Random(hash((self.outdir.name, rh, t)) & 0xFFFF)
        cell = dict(rh=rh, t=t, repeat=self.campaign.count(rh, t))
        self.set_phase(kind="settle", step=self.step_index, rh=rh, t=t, co2=None, pass_=cell["repeat"], session=self.outdir.name)
        ok, reached = self._goto(t, rh, P.co2_levels[0], new_rh=first_in_block)
        if not ok:
            self.event("cell_skipped", rh=rh, t=t, reason="condensation guard"); return False
        top = max(P.co2_levels)
        for nominal in P.co2_levels:
            jit = rng.uniform(-P.co2_jitter_ppm, P.co2_jitter_ppm) if 0 < nominal < top else 0.0
            self._co2_step(cell, round(nominal + jit, 1), nominal, "co2")
        return True

    # -- the session --------------------------------------------------------- #
    def run(self, resume: bool = True) -> str:
        P = self.P
        self.step_index = 0
        self.status = "running"
        self.start_logger()
        self.licor.set(flow=P.flow_umol_s)
        pending = [tuple(c) for c in self.plan["cells"] if list(c) not in self.plan["done"]]
        self.event("session_started", cells=pending, estimate_h=round(self.plan["estimate_s"] / 3600, 2),
                   budget_h=P.session_budget_h, resumed=bool(self.plan["done"]))
        measured = []
        self.end_reason = None
        remaining = lambda: [c for c in pending if list(c) not in self.plan["done"]]
        try:
            if not self.plan["done"]:
                self._anchor_pair("start")
            last_rh = None
            for rh, t in pending:
                self._check_stop()
                elapsed = time.monotonic() - self.t_session0
                cost = self.campaign.est_cell_s() + (P.est_rh_change_s if rh != last_rh else P.est_t_change_s) + P.est_anchor_s
                if elapsed + cost > P.session_budget_h * 3600:
                    self.event("budget_stop", elapsed_h=round(elapsed / 3600, 2), next_cell=[rh, t])
                    self.end_reason = "budget"
                    break
                t0 = time.monotonic()
                ok = self._measure_cell(rh, t, first_in_block=(rh != last_rh))
                self.campaign.record_cell(rh, t, self.outdir.name, time.monotonic() - t0, ok)
                self.plan["done"].append([rh, t]); self._save_plan(); measured.append((rh, t, ok))
                last_rh = rh
            self.end_reason = self.end_reason or "complete"
            self.status = "done"
            self._end_anchor(self.end_reason)                    # the planned end of a session always has one
            self.event("session_done", measured=measured, reason=self.end_reason)
        except SessionStop as stop:                               # STOP file or a watchdog: clean end, then the end anchor
            self.end_reason = stop.reason
            self.status = "stopped"
            self.event("session_stopped", reason=stop.reason, measured=measured, pending=remaining(), **stop.detail)
            if P.end_anchor_on_stop:
                self._end_anchor(stop.reason)
        except KeyboardInterrupt:                                 # operator: no end anchor unless asked for
            self.end_reason = "interrupt"
            self.status = "interrupted"
            self.event("session_interrupted", measured=measured, pending=remaining())
            if P.end_anchor_on_interrupt:
                self._end_anchor("interrupt")
        finally:
            self.set_phase(kind="idle", step=None)
            try:
                self.licor.set(co2_s=P.ambient_restore_ppm, tair=P.anchor["t"], rh_air=P.anchor["rh"])
                self.event("chamber_restored", co2_s=P.ambient_restore_ppm)
            except Exception as e:
                self.event("chamber_restore_failed", error=str(e))
            self.stop_logger()
            self.campaign.record_session({"folder": self.outdir.name, "status": self.status, "end_reason": self.end_reason,
                                          "end_anchor": any(a["tag"] == "end" for a in self.anchor_results),
                                          "started": self.plan["started"], "ended": datetime.now().isoformat(timespec="seconds"),
                                          "cells": [list(m[:2]) for m in measured], "anchors": self.anchor_results,
                                          "cycles": self.cycle})
        return self.status


if __name__ == "__main__":
    # mock dry run: two sessions of a compressed campaign, then the ledger status
    import tempfile, shutil
    tmp = Path(tempfile.gettempdir()) / "co2dot_campaign_mock"
    if tmp.exists(): shutil.rmtree(tmp)
    lic = MockLicor(tau_s={"co2": 1.0, "t": 1.0, "rh": 1.0})
    camp = Campaign(tmp, Protocol3.quick(), repeats=2)
    dots = mock_dots(2, lic)
    camp.freeze_settings(dots, led_ma=4, autotune_target=None, gain=6, atime=100, astep=999)
    print(camp.status())
    for k in range(2):
        s = Session(camp, dots, lic, seed=k)
        print("session", k, "->", s.run())
    print(camp.status())
    from co2dot_calib import load_run, settled_points
    import pandas as pd
    for sess in sorted(p for p in tmp.iterdir() if p.is_dir()):
        cyc, ev, meta = load_run(sess)
        sp = settled_points(cyc, last_s=5)
        print(sess.name, cyc.shape, ev["kind"].value_counts().to_dict())
        print(sp[["phase_step", "label", "kind", "pass_", "t_set", "rh_set", "co2_set", "n", "lr_630_515"]].head(8).round(3).to_string())
