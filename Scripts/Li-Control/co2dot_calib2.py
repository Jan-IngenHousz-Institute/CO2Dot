"""
co2dot_calib2.py -- CO2Dot calibration protocol 2 on the LI-6800 (steady-state windows).

Differences from co2dot_calib.py (protocol 1):
  * LED at the driver minimum (4 mA); gain and atime auto-tuned once per device at
    start so the brightest channel sits near a target count (`autotune`).
  * No background logger. The scheduler waits until T, RH and CO2 are within
    tolerance and then records one 60 s window at 5 s cadence (one JSON line per
    sample in cycles.jsonl, same format as protocol 1, so load_run() works).
  * 0 ppm counts as reached below `co2_zero_tol_ppm` (15), the top level below
    `co2_top_tol_ppm` (40); nothing waits for the LI-6800's own stability flags.
  * The grid repeats (direction reversed every other pass) until interrupted.
    Ctrl-C stops cleanly; run() again resumes at the checkpointed pass/step.
  * Jitter (+-co2_jitter_ppm) only on the in-between CO2 levels, never on 0 or the top.

    dots = connect_dots(labels={...})
    tune = autotune(dots, led_ma=4, target_counts=30000)
    run  = Run2(outdir, dots, LicorLive(li), Protocol2(), led_ma=4, meta_extra={"autotune": tune})
    run.run()

Dry run:  lic = MockLicor(); Run2(outdir, mock_dots2(2, lic), lic, Protocol2.quick()).run()
"""
from __future__ import annotations

import json
import math
import random
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from co2dot_calib import (CHANNELS, AS7341_FULL_SCALE, DotHandle, Protocol, Run, MockDot, MockLicor,
                          dew_point_c, _json_default)

LED_MA_MIN = 4                                  # AS7341 driver: 4 mA minimum, 2 mA steps, 20 mA max
GAIN_MAX = {"AS7341": 10, "AS7343": 12}         # analog-gain enum ordinal; each step is 2x


# ----------------------------------------------------------------------------- #
# spectrometer auto-tune
# ----------------------------------------------------------------------------- #
def _tune_one(dot, led_ma: int, target: float, tol: float, astep: int, atime_min: int, atime_max: int,
              gain0: int, atime0: int, n: int, max_iter: int) -> Dict[str, Any]:
    rec: Dict[str, Any] = {"led_ma": led_ma, "astep": astep, "target": target, "history": [], "warnings": [], "ok": False}
    model = (dot.spec_status().get("model") or "AS7341")
    gmax = GAIN_MAX.get(model, 10)
    gain, atime = int(gain0), int(atime0)
    seen = set()
    peak = 0.0
    for it in range(max_iter):
        dot.set_spectrometer(atime=atime, astep=astep, gain=gain)
        fl = [dot.spec_flash(led_ma) for _ in range(n)]
        lit = {c: statistics.mean(f["lit"][c] for f in fl) for c in CHANNELS if c in fl[0]["lit"]}
        ch = max(lit, key=lit.get)
        peak = lit[ch]
        rec["history"].append({"it": it, "gain": gain, "atime": atime, "peak": round(peak), "channel": ch})
        rec.update(model=model, gain=gain, atime=atime, peak_lit=round(peak), peak_channel=ch,
                   frac_fs=round(peak / AS7341_FULL_SCALE, 3), lit={c: round(v) for c, v in lit.items()})
        if peak < 20:
            rec["warnings"].append("no LED signal (peak < 20 counts): LED off, no target, or device fault")
            break
        if peak >= 0.97 * AS7341_FULL_SCALE:
            f = 0.35                                    # clipped: real factor unknown, back off hard
        else:
            f = target / peak
        if abs(f - 1.0) <= tol:
            rec["ok"] = True
            break
        a_eff = (atime + 1) * f                         # integration time is linear in (atime + 1)
        while a_eff > atime_max + 1 and gain < gmax:    # fold powers of two into the gain
            gain += 1; a_eff /= 2.0
        while a_eff < atime_min + 1 and gain > 0:
            gain -= 1; a_eff *= 2.0
        atime = int(min(max(round(a_eff) - 1, atime_min), atime_max))
        if (gain, atime) in seen:                       # converged on a quantization limit
            rec["ok"] = abs(f - 1.0) <= 2 * tol
            break
        seen.add((gain, atime))
    else:
        rec["warnings"].append(f"did not converge in {max_iter} iterations")
    if not rec["ok"] and peak >= 20:
        if gain >= gmax and atime >= atime_max and peak < target:
            rec["warnings"].append("LED too weak: below target even at maximum gain and atime")
        elif gain <= 0 and atime <= atime_min and peak > target:
            rec["warnings"].append("too bright even at minimum gain and atime")
    return rec


def autotune(dots: Dict[str, DotHandle], led_ma: int = LED_MA_MIN, target_counts: float = 30000.0,
             tol: float = 0.05, astep: int = 999, atime_min: int = 65, atime_max: int = 255,
             gain0: int = 5, atime0: int = 100, n: int = 2, max_iter: int = 8,
             verbose: bool = True) -> Dict[str, Dict[str, Any]]:
    """Per device: set the gain and atime so the brightest lit channel reads ~target_counts at led_ma.

    Strategy: astep stays fixed; atime_min=65 keeps (atime+1)*(astep+1) >= 65535 so the ADC full
    scale is the 16-bit maximum. Powers of two go into the gain, the remainder into atime.
    Returns {device_id: {gain, atime, astep, led_ma, peak_lit, peak_channel, frac_fs, ok, ...}}.
    Pass the result as meta_extra={"autotune": ...} to Run2 so it is stored with the run.
    """
    report = {}
    for dev_id, h in dots.items():
        try:
            rec = _tune_one(h.dot, led_ma, target_counts, tol, astep, atime_min, atime_max, gain0, atime0, n, max_iter)
        except Exception as e:
            rec = {"ok": False, "warnings": [f"FAILED: {e}"], "history": []}
        rec.update(port=h.port, label=h.label)
        report[dev_id] = rec
        if verbose:
            print(f"[{h.label} {dev_id} {h.port}] gain={rec.get('gain')} atime={rec.get('atime')} astep={astep} "
                  f"led={led_ma} mA -> peak {rec.get('peak_lit')} counts on {rec.get('peak_channel')} "
                  f"({100 * rec.get('frac_fs', 0):.0f} % FS) after {len(rec['history'])} iterations"
                  f"{'' if rec['ok'] else '  NOT OK'}")
            for w in rec["warnings"]:
                print("   WARNING:", w)
    return report


# ----------------------------------------------------------------------------- #
# protocol 2
# ----------------------------------------------------------------------------- #
@dataclass
class Protocol2(Protocol):
    """Grid of protocol 2. RH blocks outermost, T inside, CO2 steps innermost; repeats until interrupted.

    `passes` <= 0 means repeat forever; odd passes walk the grid in reverse order.
    Each CO2 step: setpoint -> wait for tolerance (timeout co2_timeout_s) -> one
    sample_window_s window at sample_period_s cadence.
    """
    rh_levels: Tuple[float, ...] = (20, 30, 40, 50, 60)
    t_levels: Tuple[float, ...] = (17, 22, 27)
    co2_levels: Tuple[float, ...] = (0, 300, 800, 2000, 0)
    rh_cap_by_t: Dict[float, float] = field(default_factory=dict)
    co2_jitter_ppm: float = 30.0            # only on levels strictly between min and max
    co2_tol_ppm: float = 3.0                # in-between levels: max(this, 1 %)
    co2_zero_tol_ppm: Optional[float] = 15.0
    co2_top_tol_ppm: float = 40.0           # the top level is accepted within this, no stability wait
    co2_timeout_s: float = 300.0
    t_tol_c: float = 0.5
    t_timeout_s: float = 1500.0
    rh_tol_pct: float = 3.0
    rh_timeout_s: float = 1800.0
    sample_period_s: float = 5.0
    sample_window_s: float = 60.0
    passes: int = 0                         # 0 = until interrupted
    guard_wait_s: float = 600.0
    cooldown_dry: bool = True
    max_dew_c: Optional[float] = 19.5       # humidifier ceiling (19.7 C dew observed 2026-10-08)
    settle_at_next_co2: bool = True
    flow_umol_s: float = 400.0

    @classmethod
    def quick(cls) -> "Protocol2":
        """Compressed protocol for mock dry runs."""
        return cls(rh_levels=(40, 60), t_levels=(20, 28), co2_levels=(0, 300, 2000, 0), co2_timeout_s=20,
                   t_timeout_s=30, rh_timeout_s=30, sample_period_s=0.5, sample_window_s=3, passes=1,
                   max_dew_c=None, cooldown_dry=False, guard_wait_s=0)

    def rh_allowed(self, t: float, rh: float) -> Tuple[bool, str]:
        ok, why = super().rh_allowed(t, rh)
        if ok and self.max_dew_c is not None and dew_point_c(t, rh) > self.max_dew_c:
            return False, f"dew point {dew_point_c(t, rh):.1f} C > max_dew_c {self.max_dew_c} C"
        return ok, why

    def steps(self, pass_: int = 0) -> List[Dict[str, Any]]:
        """Flat step list of ONE pass (reversed order on odd passes)."""
        rng = random.Random(f"{self.seed}-{pass_}")
        out: List[Dict[str, Any]] = []
        lo, hi = min(self.co2_levels), max(self.co2_levels)

        def add(kind, rh, t, co2, **extra):
            out.append(dict(index=len(out), kind=kind, pass_=pass_, rh=rh, t=t, co2=co2,
                            dwell_s=self.sample_window_s, **extra))

        rhs, ts = list(self.rh_levels), list(self.t_levels)
        if pass_ % 2 == 1:
            rhs.reverse(); ts.reverse()
        for rh in rhs:
            for t in ts:
                ok, why = self.rh_allowed(t, rh)
                if not ok:
                    add("skip", rh, t, None, reason=why); continue
                add("cell_settle", rh, t, None)
                for sp in self.co2_levels:
                    jit = rng.uniform(-self.co2_jitter_ppm, self.co2_jitter_ppm) if lo < sp < hi else 0.0
                    add("co2", rh, t, round(sp + jit, 1), co2_nominal=sp)
        return out

    def estimate(self) -> Dict[str, float]:
        """Rough duration of ONE pass (settling at ~40 % of each timeout) and mixer-active hours."""
        steps = self.steps(0)
        total = mixer = 0.0
        last_rh = last_t = None
        for s in steps:
            if s["kind"] == "skip":
                continue
            if s["rh"] != last_rh:
                total += 0.4 * self.rh_timeout_s; last_rh = s["rh"]
            if s["t"] != last_t:
                total += 0.4 * self.t_timeout_s; last_t = s["t"]
            if s["kind"] == "co2":
                dt = 0.4 * self.co2_timeout_s + self.sample_window_s
                total += dt
                if s["co2"] and s["co2"] > 0:
                    mixer += dt
        return {"hours_per_pass": round(total / 3600, 1), "mixer_hours_per_pass": round(mixer / 3600, 1),
                "cells": len({(s["rh"], s["t"]) for s in steps if s["kind"] == "co2"}),
                "co2_steps": sum(s["kind"] == "co2" for s in steps), "skipped": sum(s["kind"] == "skip" for s in steps)}


# ----------------------------------------------------------------------------- #
# run 2: synchronous scheduler with measurement windows
# ----------------------------------------------------------------------------- #
def _sleep(seconds: float) -> None:
    """Sleep in <= 1 s chunks so Ctrl-C (KeyboardInterrupt) lands promptly on Windows."""
    end = time.monotonic() + seconds
    while True:
        rem = end - time.monotonic()
        if rem <= 0:
            return
        time.sleep(min(1.0, rem))


class Run2(Run):
    """Run directory: cycles.jsonl (one line per 5 s sample), events.jsonl, meta.json, checkpoint.json."""

    def __init__(self, outdir, dots: Dict[str, DotHandle], licor, protocol: Protocol2, *,
                 led_ma: int = LED_MA_MIN, meta_extra: Optional[Dict[str, Any]] = None, verbose: bool = True):
        super().__init__(outdir, dots, licor, protocol, cadence_s=protocol.sample_period_s, led_ma=led_ma,
                         meta_extra=dict(meta_extra or {}, protocol_version=2), verbose=verbose)
        self._write_meta({"spectrometer_settings": {d: h.dot.spec_status() for d, h in dots.items()}})
        path = self.outdir / "cycles.jsonl"
        if path.exists():                                   # resume in a fresh object: keep cycle ids unique
            try:
                last = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
                self.cycle = json.loads(last[-1])["cycle"] + 1 if last else 0
            except Exception:
                self.cycle = 0

    # -- checkpoint (pass + step) ------------------------------------------- #
    def _checkpoint2(self, pass_: int, next_index: int) -> None:
        (self.outdir / "checkpoint.json").write_text(json.dumps(
            {"pass": pass_, "next_index": next_index, "status": self.status,
             "ts": datetime.now().isoformat(timespec="seconds")}), encoding="utf-8")

    def _load_checkpoint2(self) -> Tuple[int, int]:
        p = self.outdir / "checkpoint.json"
        if not p.exists():
            return 0, 0
        cp = json.loads(p.read_text(encoding="utf-8"))
        return int(cp.get("pass", 0)), int(cp.get("next_index", 0))

    # -- measurement window --------------------------------------------------- #
    def _measure_window(self, phase: Dict[str, Any]) -> int:
        """Record sample_window_s / sample_period_s samples (LI-6800 + every device). Returns n written."""
        P = self.protocol
        n = max(int(round(P.sample_window_s / P.sample_period_s)), 1)
        t0 = time.monotonic()
        late = 0
        with (self.outdir / "cycles.jsonl").open("a", encoding="utf-8") as f:
            for k in range(n):
                wait = t0 + k * P.sample_period_s - time.monotonic()
                if wait > 0:
                    _sleep(wait)
                elif k and wait < -P.sample_period_s:
                    late += 1
                t_start = time.monotonic()
                try:
                    licor = self.licor.read()
                except Exception as e:
                    licor = {"error": str(e)}
                line = {"ts": datetime.now().isoformat(timespec="milliseconds"), "t_mono": t_start,
                        "cycle": self.cycle, "phase": dict(phase, sample=k),
                        "licor": licor, "devices": {d: self._read_device(h, k == 0) for d, h in self.dots.items()}}
                f.write(json.dumps(line, default=_json_default) + "\n")
                self.cycle += 1
                peak = max((max(r["spec_flash"]["lit"].values()) for r in line["devices"].values() if r.get("spec_flash")),
                           default=0)
                if peak >= 0.95 * AS7341_FULL_SCALE:
                    self.event("saturation", step=phase.get("step"), peak_lit=peak)
        if late:
            self.event("sampling_late", step=phase.get("step"), late_samples=late)
        return n

    # -- scheduler ----------------------------------------------------------- #
    def run(self, resume: bool = True, start: Optional[Tuple[int, int]] = None) -> str:
        """Walk the grid until Ctrl-C (or `passes` done). Returns 'done', 'paused' or 'interrupted'."""
        P: Protocol2 = self.protocol
        p, i = start if start is not None else (self._load_checkpoint2() if resume else (0, 0))
        self.status = "running"
        self.event("run_started", pass_=p, from_index=i, estimate=P.estimate())
        cur_rh = cur_t = None
        co2_fail_streak = 0
        reached_above_ambient = False
        try:
            self.licor.set(flow=P.flow_umol_s)
            while P.passes <= 0 or p < P.passes:
                steps = P.steps(p)
                if i == 0:
                    self.event("pass_started", pass_=p, steps=len(steps))
                while i < len(steps):
                    s = steps[i]
                    self._checkpoint2(p, i)
                    if s["kind"] == "skip":
                        self.event("cell_skipped", pass_=p, index=i, rh=s["rh"], t=s["t"], reason=s["reason"])
                        i += 1; continue

                    # ---- new (RH, T) cell: set T, RH and the cell's first CO2 together, wait for all three
                    if s["rh"] != cur_rh or s["t"] != cur_t:
                        nxt = steps[i + 1] if s["kind"] == "cell_settle" and i + 1 < len(steps) else s
                        first_co2 = float(nxt["co2"]) if nxt.get("co2") is not None else None
                        cooling = cur_t is not None and s["t"] < cur_t
                        drier = cur_rh is not None and s["rh"] < cur_rh
                        dried = cooling and P.cooldown_dry and self._dry_before_cooling(i, s["t"])
                        rh_after_t = cooling and P.cooldown_dry and (dried or not drier)
                        if first_co2 is not None:
                            self.licor.set(co2_s=first_co2)
                        if rh_after_t:                      # cooling: keep the dry RH until T is reached
                            self.licor.set(tair=s["t"])
                        elif drier:
                            self.licor.set(rh_air=s["rh"]); self.licor.set(tair=s["t"])
                        else:
                            self.licor.set(tair=s["t"]); self.licor.set(rh_air=s["rh"])
                        self.event("setpoint", pass_=p, index=i, tair=s["t"], rh_air=s["rh"], co2_s=first_co2)
                        ok_t, dt_t = self._wait("Tchamber", s["t"], P.t_tol_c, P.t_timeout_s)
                        self.event("t_reached" if ok_t else "t_timeout", index=i, t=s["t"], after_s=round(dt_t),
                                   Tchamber=self.licor.get("Tchamber"))
                        if rh_after_t:
                            self.licor.set(rh_air=s["rh"])
                        ok_rh, dt_rh = self._wait("RHcham", s["rh"], P.rh_tol_pct, P.rh_timeout_s)
                        self.event("rh_reached" if ok_rh else "rh_timeout", index=i, rh=s["rh"], after_s=round(dt_rh),
                                   RHcham=self.licor.get("RHcham"))
                        guard = self._dewpoint_guard(s["t"])
                        t_g = time.monotonic()
                        while guard and time.monotonic() - t_g < P.guard_wait_s:
                            _sleep(20.0); guard = self._dewpoint_guard(s["t"])
                        if guard:
                            self.event("condensation_guard", pass_=p, index=i, detail=guard, action="skip cell",
                                       waited_s=round(time.monotonic() - t_g))
                            cur_rh, cur_t = s["rh"], s["t"]
                            j = i
                            while j < len(steps) and steps[j]["rh"] == s["rh"] and steps[j]["t"] == s["t"]:
                                j += 1
                            i = j; continue
                        if time.monotonic() - t_g > 1.0:
                            self.event("condensation_guard_cleared", index=i, waited_s=round(time.monotonic() - t_g))
                        self.event("cell_settled", pass_=p, index=i, rh=s["rh"], t=s["t"], t_ok=ok_t, rh_ok=ok_rh)
                        cur_rh, cur_t = s["rh"], s["t"]
                    if s["kind"] == "cell_settle":
                        i += 1; continue

                    # ---- CO2 step: setpoint -> tolerance (no stability flag) -> one measurement window
                    sp = float(s["co2"]); nominal = s.get("co2_nominal", sp)
                    self.licor.set(co2_s=sp)
                    if sp == 0:
                        tol = P.co2_zero_tol_ppm or P.co2_tol_ppm
                    elif nominal >= max(P.co2_levels):
                        tol = P.co2_top_tol_ppm
                    else:
                        tol = max(P.co2_tol_ppm, 0.01 * sp)
                    ok, dt = self._wait("CO2_s", sp, tol, P.co2_timeout_s)
                    co2_now = self.licor.get("CO2_s")
                    self.event("co2_reached" if ok else "co2_timeout", pass_=p, index=i, co2=sp, tol=tol,
                               after_s=round(dt), CO2_s=co2_now)
                    if sp > P.ambient_restore_ppm + 50:             # cartridge watchdog (as protocol 1)
                        if ok:
                            reached_above_ambient = True; co2_fail_streak = 0
                        elif isinstance(co2_now, (int, float)) and co2_now < 0.8 * sp:
                            co2_fail_streak += 1
                            self.event("cartridge_suspected", index=i, co2=sp, CO2_s=co2_now, streak=co2_fail_streak)
                            if co2_fail_streak >= 2 and reached_above_ambient:
                                self.status = "paused"; self._checkpoint2(p, i)
                                self.event("run_paused", reason="CO2 cartridge probably empty: replace it and call run() again")
                                return self.status
                    phase = dict(kind="co2", step=i, pass_=p, rh=s["rh"], t=s["t"], co2=sp, co2_nominal=nominal,
                                 transition=False, reached=ok)
                    n = self._measure_window(phase)
                    guard = self._dewpoint_guard(s["t"])
                    if guard:
                        self.event("condensation_guard", index=i, detail=guard, action="continue, flagged")
                    self.event("step_done", pass_=p, index=i, co2=sp, rh=s["rh"], t=s["t"], samples=n, reached=ok)
                    i += 1
                self.event("pass_done", pass_=p)
                p += 1; i = 0
                self._checkpoint2(p, i)
            self.status = "done"; self._checkpoint2(p, i)
            self.event("run_done", cycles=self.cycle)
        except KeyboardInterrupt:
            self.status = "interrupted"; self._checkpoint2(p, i)
            self.event("run_interrupted", pass_=p, at_index=i)
        finally:
            try:
                self.licor.set(co2_s=P.ambient_restore_ppm, tair=P.anchor["t"], rh_air=P.anchor["rh"])
                self.event("chamber_restored", co2_s=P.ambient_restore_ppm)
            except Exception as e:
                self.event("chamber_restore_failed", error=str(e))
        return self.status

    def close(self) -> None:
        for h in self.dots.values():
            h.close()


# ----------------------------------------------------------------------------- #
# mocks (signal scales with gain, atime and LED current so autotune can be exercised)
# ----------------------------------------------------------------------------- #
class MockDot2(MockDot):
    def spec_flash(self, led_ma=LED_MA_MIN):
        self._advance()
        g = 2 ** (self.cfg["gain"] - 5) * (self.cfg["atime"] + 1) / 101.0 * max(led_ma, LED_MA_MIN) / 10.0
        dark, lit, diff = {}, {}, {}
        for c in CHANNELS:
            a = (1 - self.x) * self._E0[c] + self.x * self._E1[c]
            s = self.scale * g * self._BASE[c] * 10 ** (-a)
            d = 3 * g; s_n = s + random.gauss(0, 0.5 * math.sqrt(s) + 1); d_n = d + random.gauss(0, 1)
            dark[c] = int(max(d_n, 0)); lit[c] = int(min(max(s_n + d_n, 0), AS7341_FULL_SCALE))
            diff[c] = max(lit[c] - dark[c], 0)
        return {"led_current": led_ma, "model": "AS7341", "dark": dark, "lit": lit, "diff": diff}


def mock_dots2(n: int = 2, licor: Optional[MockLicor] = None) -> Dict[str, DotHandle]:
    licor = licor or MockLicor()
    return {f"MOCK:{i:02d}": DotHandle(MockDot2(licor, scale=1.0 - 0.15 * i), f"MOCK{i}", f"MOCK:{i:02d}", f"M{i}",
                                       {"version": "mock"}) for i in range(n)}


if __name__ == "__main__":
    from co2dot_calib import load_run, settled_points
    out = Path("co2dot_data") / (datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + "_mock_dryrun2")
    lic = MockLicor(tau_s={"co2": 2.0, "t": 2.0, "rh": 2.0})
    dots = mock_dots2(2, lic)
    autotune(dots, led_ma=4, target_counts=30000)
    P = Protocol2.quick()
    print(P.feasibility_table())
    run = Run2(out, dots, lic, P, led_ma=4)
    print("status:", run.run())
    cyc, ev, meta = load_run(out)
    print(cyc.shape, ev["kind"].value_counts().to_dict())
    print(settled_points(cyc, last_s=3)[["phase_step", "label", "co2_set", "n", "lit_f7_630", "lr_630_515"]].head(12))
