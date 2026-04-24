"""
li_worker.py — QThread that owns the paramiko SSH/SFTP session to the LI-6800.

paramiko is imported at module top so that a missing package raises ImportError
at `from li_worker import LiWorker` time (caught on the main thread in
_on_li_connect), not later on this worker thread.

Mirrors the SerialWorker pattern: a tight run loop drains an outgoing job queue,
uploads each job's JSON command to REMOTE_CMD via atomic sftp posix_rename,
polls REMOTE_ACK until a matching cmd_id appears, and emits a normalized ack
dict on the main thread via Qt's queued signal delivery.
"""

from __future__ import annotations

import json
import queue
import socket
import threading
import time
from io import BytesIO
from typing import Any

import paramiko
from PySide6.QtCore import QThread, Signal

from li_control import (
    REMOTE_ACK,
    REMOTE_CMD,
    REMOTE_TMP,
    LiConfig,
    LiSetpoints,
    build_stop_cmd,
    new_cmd_id,
    normalize_ack,
)


class LiWorker(QThread):
    connected      = Signal(str)       # host label
    disconnected   = Signal()
    ack_received   = Signal(dict)      # normalized ack
    error_received = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cfg: LiConfig | None = None
        self._client: paramiko.SSHClient | None = None
        self._sftp: paramiko.SFTPClient | None = None
        self._jobs: queue.Queue = queue.Queue()
        self._running = False
        self._abort = threading.Event()

    # ------------------------------------------------------------------
    # Public API (main thread)
    # ------------------------------------------------------------------

    def open_connection(self, cfg: LiConfig) -> None:
        self._cfg = cfg
        self._running = True
        self._abort.clear()
        self.start()

    def close_connection(self) -> None:
        self._running = False
        self._abort.set()
        # Unblock any Queue.get() wait.
        self._jobs.put({"__stop__": True})
        if not self.wait(3000):
            self.terminate()
            self.wait(1000)

    def send_setpoints(self, sp: LiSetpoints, *, timeout_s: float | None = None) -> str:
        cid = new_cmd_id()
        self._jobs.put({
            "kind": "measure",
            "cmd_id": cid,
            "cmd": sp.to_cmd(cid),
            "timeout_s": timeout_s,
        })
        return cid

    def send_stop(self) -> str:
        cid = new_cmd_id()
        self._jobs.put({
            "kind": "stop",
            "cmd_id": cid,
            "cmd": build_stop_cmd(cid),
            "timeout_s": 30.0,
        })
        return cid

    def abort_current(self) -> None:
        self._abort.set()

    # ------------------------------------------------------------------
    # Run loop (worker thread)
    # ------------------------------------------------------------------

    def run(self) -> None:
        cfg = self._cfg
        if cfg is None:
            self.error_received.emit("LiWorker: no config")
            self._running = False
            self.disconnected.emit()
            return

        if not self._connect(cfg):
            self._running = False
            self.disconnected.emit()
            return

        self.connected.emit(cfg.host)

        while self._running:
            try:
                job = self._jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            if job.get("__stop__"):
                break
            self._abort.clear()
            try:
                self._process_job(job, cfg)
            except (paramiko.SSHException, OSError) as exc:
                self.error_received.emit(f"Li SSH error: {exc}")
                if not self._reconnect(cfg):
                    break

        self._close_handles()
        self._running = False
        self.disconnected.emit()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _connect(self, cfg: LiConfig) -> bool:
        try:
            client = paramiko.SSHClient()
            if cfg.host_key_policy == "reject":
                client.set_missing_host_key_policy(paramiko.RejectPolicy())
            else:
                client.load_system_host_keys()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                hostname=cfg.host,
                port=cfg.port,
                username=cfg.username,
                key_filename=cfg.key_filename,
                look_for_keys=True,
                allow_agent=True,
                timeout=10.0,
            )
            transport = client.get_transport()
            if transport is not None:
                transport.set_keepalive(int(cfg.keepalive_s))
            sftp = client.open_sftp()
        except Exception as exc:
            self.error_received.emit(f"Li connect failed: {exc}")
            return False
        self._client = client
        self._sftp = sftp
        return True

    def _reconnect(self, cfg: LiConfig) -> bool:
        self._close_handles()
        self.error_received.emit("Li reconnecting…")
        return self._connect(cfg)

    def _close_handles(self) -> None:
        if self._sftp is not None:
            try:
                self._sftp.close()
            except Exception:
                pass
            self._sftp = None
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    # ------------------------------------------------------------------
    # Job processing
    # ------------------------------------------------------------------

    def _process_job(self, job: dict, cfg: LiConfig) -> None:
        if self._sftp is None:
            self.error_received.emit("Li not connected")
            return

        cmd = job["cmd"]
        cmd_id = job["cmd_id"]
        timeout_s = job.get("timeout_s")
        effective_timeout = timeout_s if timeout_s is not None else cfg.ack_timeout_s

        payload = json.dumps(cmd, separators=(",", ":")).encode("utf-8")
        self._sftp.putfo(BytesIO(payload), REMOTE_TMP)
        self._sftp.posix_rename(REMOTE_TMP, REMOTE_CMD)

        deadline = time.monotonic() + float(effective_timeout)
        while time.monotonic() < deadline:
            if self._abort.is_set():
                self.error_received.emit("Li job aborted")
                return
            if not self._running:
                return
            try:
                with self._sftp.open(REMOTE_ACK, "r") as f:
                    data = f.read()
            except (FileNotFoundError, IOError):
                time.sleep(cfg.poll_interval_s)
                continue
            try:
                raw = json.loads(data)
            except (json.JSONDecodeError, ValueError):
                time.sleep(cfg.poll_interval_s)
                continue
            if not isinstance(raw, dict):
                time.sleep(cfg.poll_interval_s)
                continue
            if str(raw.get("cmd_id", "")) != cmd_id:
                time.sleep(cfg.poll_interval_s)
                continue
            self.ack_received.emit(normalize_ack(raw))
            return

        self.error_received.emit(f"Li timeout (cmd_id={cmd_id})")
