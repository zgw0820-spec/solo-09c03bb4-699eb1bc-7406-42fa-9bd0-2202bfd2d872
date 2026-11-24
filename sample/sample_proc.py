#!/usr/bin/env python3
"""Single-threaded stand-in for a 收包 (packet-receive) worker.

The process owns exactly one thread.  It alternates between a short busy
period and an idle wait that doubles as the control-channel poll, so it does
real work on whichever core it is bound to without spawning helpers.

It never changes its own affinity - rebinding is the orchestrator's job.  The
affinity it reports comes from ``/proc/self/status`` (``Cpus_allowed_list``).

Protocol (one JSON object per line on stdin, one per line on stdout):

    {"cmd": "ping"}   -> {"ok": true, "pid": ..., "cpus": "...", ...}
    {"cmd": "quit"}   -> {"ok": true} then exit
"""

from __future__ import annotations

import json
import os
import select
import sys
import time

# Works both from the dev checkout (dir on sys.path) and inside the image
# where /app is the working directory and holds the same tree.
try:
    from drill.cpulist import format_cpu_list, parse_cpu_list
    from drill.kernel import cpu_pool, rss_kib, thread_count
except ImportError:  # pragma: no cover - direct script runs
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from drill.cpulist import format_cpu_list, parse_cpu_list
    from drill.kernel import cpu_pool, rss_kib, thread_count

NAME = os.environ.get("SAMPLE_NAME", "sample")
BUSY_MS = int(os.environ.get("SAMPLE_BUSY_MS", "120"))
IDLE_MS = int(os.environ.get("SAMPLE_IDLE_MS", "400"))


def _status_fields() -> dict[str, str]:
    fields: dict[str, str] = {}
    with open("/proc/self/status", encoding="utf-8") as handle:
        for line in handle:
            if ":" in line:
                key, value = line.split(":", 1)
                fields[key.strip()] = value.strip()
    return fields


def status() -> dict:
    """Report this process's own kernel view of its affinity.

    The mask is parsed from ``Cpus_allowed_list`` in /proc/self/status, so
    this is an independent second read path beside the orchestrator's
    ``/proc/<pid>`` scan.
    """

    fields = _status_fields()
    cpus = parse_cpu_list(fields.get("Cpus_allowed_list", ""))
    return {
        "ok": True,
        "name": NAME,
        "pid": os.getpid(),
        "tid": os.getpid(),  # a single-threaded process is its own leader
        "cpus": format_cpu_list(cpus),
        "cpus_list": sorted(cpus),
        "raw_status_value": fields.get("Cpus_allowed_list", ""),
        "source": "/proc/self/status:Cpus_allowed_list",
        "threads": thread_count(os.getpid()),
        "state": fields.get("State", "?").split(" ", 1)[0],
        "pool": format_cpu_list(cpu_pool()),
        "rss_kib": rss_kib(os.getpid()),
        "observed_at": time.time(),
    }


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


class LineReader:
    """Non-blocking line reader over fd 0, keeping the process single-threaded."""

    def __init__(self) -> None:
        self._buffer = b""
        self._fd = sys.stdin.fileno()
        os.set_blocking(self._fd, False)
        self.closed = False

    def poll(self, timeout: float) -> list[str]:
        """Collect complete lines that arrive within ``timeout`` seconds."""

        lines: list[str] = []
        end = time.monotonic() + max(timeout, 0)
        while True:
            while b"\n" in self._buffer:
                raw, self._buffer = self._buffer.split(b"\n", 1)
                text = raw.decode("utf-8", "replace").strip()
                if text:
                    lines.append(text)
            if lines or self.closed:
                return lines
            remaining = end - time.monotonic()
            if remaining <= 0:
                return lines
            ready, _, _ = select.select([self._fd], [], [], remaining)
            if not ready:
                return lines
            try:
                chunk = os.read(self._fd, 65536)
            except BlockingIOError:
                continue
            if not chunk:
                self.closed = True
                return lines
            self._buffer += chunk


def _burst(seconds: float) -> None:
    """Busy-spin for up to ``seconds`` so the bound core shows activity."""

    deadline = time.monotonic() + seconds
    acc = 0
    while time.monotonic() < deadline:
        acc += 1


def main() -> int:
    reader = LineReader()
    _emit({"ok": True, "ready": True, "name": NAME, "pid": os.getpid()})

    busy = max(BUSY_MS, 0) / 1000.0
    idle = max(IDLE_MS, 0) / 1000.0

    while True:
        for line in reader.poll(idle):
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                _emit({"ok": False, "error": "bad json"})
                continue
            command = request.get("cmd")
            if command == "ping":
                _emit(status())
            elif command == "quit":
                _emit({"ok": True})
                return 0
            else:
                _emit({"ok": False, "error": f"unknown command {command!r}"})
        if reader.closed:
            return 0
        if busy:
            _burst(busy)


if __name__ == "__main__":
    raise SystemExit(main())
