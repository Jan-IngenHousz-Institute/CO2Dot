"""
ext_serial_script.py — Python mini-script engine for the Serial Scripting
feature.

ScriptRunner executes user-written Python (from the panel's editor) in a
dedicated QThread via exec(). Injected helpers:

    send(cmd)           send a raw line to the external serial port
    wait(seconds)       abort-interruptible sleep
    param(name, value)  record/plot a named parameter value
    pwm(value)          send("PWM <value>") + param("pwm", value)
    dc_offset(volts)    drive the AD3 DC offset (lazy WaveForms import) and
                        param("led_V", <clamped volts>)
    print(...)          routed to the panel console
    time.sleep(s)       `time` is a proxy whose sleep() is the interruptible
                        wait; `import time` inside a script rebinds the real
                        module and re-opens the abort hole — documented.

Abort strategy (QThread.terminate() is never used on this thread — killing
exec()'d Python mid-bytecode can corrupt the interpreter):
  1. ScriptAbort derives from BaseException, so a user's `except Exception:`
     cannot swallow it while `finally:` cleanup blocks still run.
  2. Only wait() (and the time proxy's sleep) honors the abort event, and it
     reacts immediately. Other helpers stay usable after an abort so that
     `finally: pwm(0); dc_offset(0)` cleanup works. Avoid wait() inside
     cleanup — after an abort it raises again.
  3. force_abort() additionally schedules ScriptAbort asynchronously in the
     script thread (PyThreadState_SetAsyncExc), breaking pure-Python busy
     loops at the next bytecode boundary. The GUI only escalates to it after
     a grace period, so normal aborts unwind cleanly through `finally:`.
     C-blocking calls (real time.sleep, blocking I/O) delay it until they
     return — a documented residual hole.

Scripts run with full builtins: this is a lab tool operated by the script's
author; there is deliberately no sandboxing.
"""

from __future__ import annotations

import ctypes
import math
import re
import threading
import time
import traceback

from PySide6.QtCore import QThread, Signal

_PARAM_RE = re.compile(r"""\bparam\(\s*(['"])([A-Za-z_]\w*)\1""")
_NAME_RE = re.compile(r"^[A-Za-z_]\w*$")


def scan_param_names(code: str) -> list[str]:
    """Best-effort static scan of a script for the param names it will set.

    Used to freeze the extra main-record columns at record start. Literal
    param("name", ...) calls are found; pwm(...) implies "pwm" and
    dc_offset(...) implies "led_V". Dynamically-built names are missed —
    they still reach the sidecar log and the plot, just not the main file.
    """
    code = code or ""
    names = [m.group(2) for m in _PARAM_RE.finditer(code)]
    if re.search(r"\bpwm\s*\(", code):
        names.append("pwm")
    if re.search(r"\bdc_offset\s*\(", code):
        names.append("led_V")
    return list(dict.fromkeys(names))


class ScriptAbort(BaseException):
    """Raised inside the script thread to unwind on Abort. BaseException so
    a user's `except Exception:` can't swallow it; `finally:` still runs."""


class _TimeProxy:
    """time-module stand-in whose sleep() is the interruptible wait."""

    def __init__(self, runner: "ScriptRunner"):
        self._runner = runner

    def sleep(self, seconds: float) -> None:
        self._runner._wait(seconds)

    def __getattr__(self, name):
        return getattr(time, name)


class ScriptRunner(QThread):
    started_run  = Signal()
    finished_run = Signal(bool, str)          # ok, ""|"aborted by user"|traceback
    param_set    = Signal(float, str, float)  # timestamp, name, value (queued)
    info         = Signal(str)                # print() output / helper notices

    def __init__(self, worker_provider, parent=None):
        """worker_provider: ExtSerialWorker instance, or a zero-arg callable
        returning the current one (lets a script survive connect-after-Run)."""
        super().__init__(parent)
        self._worker_provider = worker_provider
        self._code_obj = None
        self.source_code = ""     # snapshot of the running script's source
        self._abort = threading.Event()
        self._tid: int | None = None

    # ---- Public API (main thread) --------------------------------------

    def run_script(self, code: str) -> None:
        """Compile and launch. Compilation happens here, on the caller's
        thread, so a SyntaxError surfaces before any thread starts."""
        self._code_obj = compile(code, "<script>", "exec")
        self.source_code = code
        self._abort.clear()
        self.start()

    def abort(self) -> None:
        """Request a stop. wait() reacts immediately; other code keeps
        running until its next wait(). Escalate with force_abort()."""
        self._abort.set()

    def force_abort(self) -> None:
        """Asynchronously raise ScriptAbort in the script thread to break
        busy loops that never call wait(). May interrupt `finally:` cleanup,
        so callers should only escalate after a grace period."""
        tid = self._tid
        if tid is None or not self.isRunning():
            return
        res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_ulong(tid), ctypes.py_object(ScriptAbort))
        if res > 1:   # shouldn't happen; undo to avoid poisoning the thread
            ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_ulong(tid), None)

    # ---- Thread body -----------------------------------------------------

    def run(self) -> None:
        self._tid = threading.get_ident()
        env = {
            "__builtins__": __builtins__,
            "send": self._send,
            "wait": self._wait,
            "param": self._param,
            "pwm": self._pwm,
            "dc_offset": self._dc_offset,
            "print": self._print,
            "time": _TimeProxy(self),
            "math": math,
        }
        self.started_run.emit()
        try:
            try:
                exec(self._code_obj, env)
            except ScriptAbort:
                self.finished_run.emit(False, "aborted by user")
            except SystemExit:
                self.finished_run.emit(True, "")
            except Exception:
                self.finished_run.emit(False, traceback.format_exc())
            else:
                self.finished_run.emit(True, "")
        except BaseException:
            # A very late force_abort() landed after the script body already
            # ended. The panel is unlocked via QThread.finished regardless.
            pass
        finally:
            self._tid = None

    # ---- Injected helpers (run on the script thread) ---------------------

    def _current_worker(self):
        wp = self._worker_provider
        return wp() if callable(wp) else wp

    def _send(self, cmd) -> None:
        worker = self._current_worker()
        if worker is None or not worker.isRunning():
            raise RuntimeError(
                "serial not connected — connect the port before send()")
        worker.send_command(str(cmd))

    def _wait(self, seconds) -> None:
        if self._abort.wait(timeout=max(0.0, float(seconds))):
            raise ScriptAbort()

    def _param(self, name, value) -> None:
        name = str(name)
        if not _NAME_RE.match(name):
            raise ValueError(
                f"invalid param name {name!r} (letters, digits, _ only)")
        self.param_set.emit(time.time(), name, float(value))

    def _pwm(self, value) -> None:
        num = float(value)
        arg = int(num) if num == int(num) else num   # "PWM 2", not "PWM 2.0"
        self._send(f"PWM {arg}")
        self._param("pwm", num)

    def _dc_offset(self, volts) -> None:
        import ad3   # lazy: WaveForms SDK only needed when a script uses it
        actual = ad3.dc_offset(volts)
        if abs(actual - float(volts)) > 1e-9:
            self.info.emit(
                f"dc_offset {float(volts):.3f} V clamped to {actual:.3f} V "
                f"(VMAX={ad3.VMAX})")
        self._param("led_V", actual)

    def _print(self, *args, sep=" ", **_kwargs) -> None:
        self.info.emit(sep.join(str(a) for a in args))
