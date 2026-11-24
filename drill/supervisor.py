"""Supervision of the sample processes (one OS process per sample).

Every sample is a separate process started by the orchestrator, with stdin
and stdout wired to line-delimited JSON.  The sample reports its own
``Cpus_allowed_list`` from ``/proc/self/status``, so the orchestrator has a
second, independent read path besides its own ``/proc/<pid>`` scan.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

from . import kernel
from .cpulist import format_cpu_list


class SampleError(RuntimeError):
    """A sample could not be started, asked, or is not what we expect."""


@dataclass
class Sample:
    name: str
    pid: int
    start_time: int | None
    process: subprocess.Popen | None = None
    _lines: "queue.Queue[dict | None]" = field(default_factory=queue.Queue)
    _reader: threading.Thread | None = None
    _ask_lock: threading.Lock = field(default_factory=threading.Lock)

    def _pump(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            for raw in self.process.stdout:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    self._lines.put(json.loads(raw))
                except json.JSONDecodeError:
                    self._lines.put({"ok": False, "error": f"non-JSON output: {raw[:120]}"})
        except (OSError, ValueError):
            pass  # stream closed underneath us during shutdown
        finally:
            self._lines.put(None)

    def ask(self, cmd: str = "ping", timeout: float = 2.0) -> dict:
        # Serialise request/response: the web app is threaded and two pages
        # asking the same sample must not consume each other's answers.
        with self._ask_lock:
            return self._ask_locked(cmd, timeout)

    def _ask_locked(self, cmd: str, timeout: float) -> dict:
        if self.process is None or self.process.stdin is None:
            raise SampleError(f"{self.name}: 无控制通道")
        if not kernel.is_alive(self.pid):
            raise SampleError(f"{self.name}: 进程 {self.pid} 已退出")
        if self.start_time is not None and kernel.process_starttime(self.pid) != self.start_time:
            raise SampleError(f"{self.name}: pid {self.pid} 已被系统复用")
        try:
            self.process.stdin.write(json.dumps({"cmd": cmd}) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise SampleError(f"{self.name}: 控制通道断开（{exc}）") from exc
        try:
            message = self._lines.get(timeout=timeout)
        except queue.Empty as exc:
            raise SampleError(f"{self.name}: 在 {timeout:.0f}s 内没有应答") from exc
        if message is None:
            raise SampleError(f"{self.name}: 控制通道已关闭")
        return message

    def stop(self, timeout: float = 2.0) -> None:
        if self.process is None:
            return
        try:
            if self.process.poll() is None:
                self.ask("quit", timeout=0.5)
        except (SampleError, OSError, ValueError):
            pass
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=timeout)
        finally:
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass


class SampleSet:
    """Starts and owns the sample processes for one drill session."""

    def __init__(
        self,
        count: int,
        pool: frozenset[int],
        *,
        python: str | None = None,
        env: dict[str, str] | None = None,
        startup_timeout: float = 5.0,
    ) -> None:
        self.count = count
        self.pool = frozenset(pool)
        self.python = python or sys.executable
        self.base_env = env
        self.startup_timeout = startup_timeout
        self.samples: dict[str, Sample] = {}
        self.notes: list[str] = []

    def _initial_bindings(self) -> list[int]:
        cpus = sorted(self.pool)
        if len(cpus) < self.count:
            raise SampleError(f"可用 CPU {format_cpu_list(cpus)} 少于样例数 {self.count}")
        # Spread the samples over distinct cores from the start.  The
        # orchestrator rebinds them explicitly right after spawning so the
        # page never shows an inherited multi-core mask.
        return cpus[: self.count]

    def start(self) -> dict[str, Sample]:
        if self.samples:
            raise SampleError("样例进程已启动")
        bindings = self._initial_bindings()
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = os.path.join(root, "sample", "sample_proc.py")
        if not os.path.exists(script):
            raise SampleError(f"找不到样例程序 {script}")

        procs: list[tuple[str, subprocess.Popen]] = []
        try:
            for index in range(self.count):
                name = f"rx-{index + 1}"
                env = dict(os.environ)
                if self.base_env:
                    env.update(self.base_env)
                env["SAMPLE_NAME"] = name
                env["PYTHONUNBUFFERED"] = "1"
                # No preexec_fn: the orchestrator is multi-threaded and forking
                # with a callback is not safe.  The child inherits our whole
                # affinity mask for a moment and is then rebound from /proc
                # to its single core before it is reported as ready.
                proc = subprocess.Popen(
                    self._command(script),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    env=env,
                    cwd=root,
                )
                procs.append((name, proc))
        except Exception as exc:  # noqa: BLE001 - cleanup must never mask it
            for _, proc in procs:
                _kill(proc)
            raise SampleError(f"启动样例进程失败：{exc}") from exc

        started: dict[str, Sample] = {}
        try:
            for index, (name, proc) in enumerate(procs):
                sample = Sample(name=name, pid=proc.pid, start_time=None, process=proc)
                sample._reader = threading.Thread(
                    target=sample._pump, name=f"{name}-reader", daemon=True
                )
                sample._reader.start()
                # Bind before waiting for "ready" so startup never publishes a
                # moment where all samples share one mask.
                kernel.set_affinity(proc.pid, {bindings[index]})
                _wait_ready(sample, self.startup_timeout)
                sample.start_time = kernel.process_starttime(proc.pid)
                if not kernel.verify_binding(proc.pid, bindings[index]):
                    raise SampleError(
                        f"{name} 启动后未能绑定到 CPU {bindings[index]}，"
                        f"内核回报 {kernel.read_affinity_text(proc.pid, proc.pid)}"
                    )
                started[name] = sample
        except Exception:
            for sample in started.values():
                sample.stop()
            for name, proc in procs:
                if name not in started:
                    _kill(proc)
            raise
        self.samples = started
        return started

    def _command(self, script: str) -> list[str]:
        return [self.python, "-u", script]

    def stop(self) -> None:
        for sample in self.samples.values():
            sample.stop()
        self.samples = {}

    def read_bindings(self) -> dict[str, dict]:
        """Live per-sample kernel view, keyed by sample name."""

        result: dict[str, dict] = {}
        for name, sample in self.samples.items():
            try:
                threads = kernel.thread_affinities(sample.pid)
                state = kernel.process_state(sample.pid)
                leader = kernel.read_affinity(sample.pid, sample.pid)
            except kernel.ProcessGoneError:
                threads = {}
                state = "X"
                leader = frozenset()
            result[name] = {
                "pid": sample.pid,
                "state": state,
                "threads": len(threads),
                "thread_cpus": [
                    {"tid": tid, "cpus": format_cpu_list(cpus)}
                    for tid, cpus in threads.items()
                ],
                "cpus": format_cpu_list(leader),
                "cpus_set": sorted(leader),
                "single_core": len(leader) == 1,
                "alive": kernel.is_alive(sample.pid),
            }
        return result

    def ask(self, name: str, cmd: str = "ping", timeout: float = 2.0) -> dict:
        sample = self.samples.get(name)
        if sample is None:
            raise SampleError(f"未知样例 {name}")
        return sample.ask(cmd, timeout=timeout)

    def snapshot_sets(self) -> dict[str, frozenset[int]]:
        """Per-sample CPU sets, read from the kernel, for planning.

        Refuses (rather than silently skipping) when a sample has died, so a
        plan is never built from a partial view of the process set.
        """

        result: dict[str, frozenset[int]] = {}
        for name, sample in self.samples.items():
            try:
                result[name] = kernel.read_affinity(sample.pid, sample.pid)
            except kernel.ProcessGoneError as exc:
                raise SampleError(f"样例 {name}（pid {sample.pid}）已退出，无法换绑") from exc
        return result


def _wait_ready(sample: Sample, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            message = sample._lines.get(timeout=0.1)
        except queue.Empty:
            if sample.process is not None and sample.process.poll() is not None:
                err = ""
                if sample.process.stderr is not None:
                    err = sample.process.stderr.read()[-400:]
                raise SampleError(f"{sample.name} 启动即退出：{err.strip()}")
            continue
        if message is None:
            raise SampleError(f"{sample.name} 未就绪即退出")
        if message.get("ready"):
            return
    raise SampleError(f"{sample.name} 在 {timeout:.0f}s 内未就绪")


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
