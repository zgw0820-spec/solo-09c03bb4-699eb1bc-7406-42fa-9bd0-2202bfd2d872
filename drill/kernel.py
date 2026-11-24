"""Orchestrator-side kernel access: process discovery and affinity writes.

Bindings are always read back from ``/proc/<pid>/status``; the orchestrator
never trusts its own bookkeeping when reporting what a process is bound to.
"""

from __future__ import annotations

import os
import re

from .cpulist import format_cpu_list, parse_cpu_list

PROC_ROOT = os.environ.get("DRILL_PROC_ROOT", "/proc")


class ProcessGoneError(RuntimeError):
    """The target process (or one of its threads) no longer exists."""


class AffinityError(RuntimeError):
    """The kernel refused an affinity change."""


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except (FileNotFoundError, ProcessLookupError) as exc:
        raise ProcessGoneError(path) from exc


def _status_fields(pid: int, tid: int | None = None) -> dict[str, str]:
    base = os.path.join(PROC_ROOT, str(pid), "task", str(tid)) if tid else os.path.join(PROC_ROOT, str(pid))
    fields: dict[str, str] = {}
    for line in _read(os.path.join(base, "status")).splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()
    return fields


def list_tids(pid: int) -> list[int]:
    task_dir = os.path.join(PROC_ROOT, str(pid), "task")
    try:
        names = os.listdir(task_dir)
    except (FileNotFoundError, ProcessLookupError) as exc:
        raise ProcessGoneError(f"pid {pid}") from exc
    return sorted(int(name) for name in names if name.isdigit())


def read_affinity(pid: int, tid: int | None = None) -> frozenset[int]:
    """Read ``Cpus_allowed_list`` for the process leader or one thread."""

    fields = _status_fields(pid, tid)
    try:
        return frozenset(parse_cpu_list(fields.get("Cpus_allowed_list", "")))
    except ValueError as exc:
        raise AffinityError(f"unparsable affinity for pid {pid}: {exc}") from exc


def read_affinity_text(pid: int, tid: int | None = None) -> str:
    return _status_fields(pid, tid).get("Cpus_allowed_list", "")


def thread_affinities(pid: int) -> dict[int, frozenset[int]]:
    return {tid: read_affinity(pid, tid) for tid in list_tids(pid)}


def thread_count(pid: int | str = "self") -> int:
    return int(_status_fields(pid).get("Threads", "0") or 0)


def set_affinity(pid: int, cpus: frozenset[int] | set[int]) -> frozenset[int]:
    """Bind every thread of ``pid`` to ``cpus`` and read the result back."""

    target = set(cpus)
    if not target:
        raise AffinityError("拒绝把进程绑定到空 CPU 集合")
    for tid in list_tids(pid):
        try:
            os.sched_setaffinity(tid, target)
        except ProcessLookupError as exc:
            raise ProcessGoneError(f"tid {tid} (pid {pid})") from exc
        except PermissionError as exc:
            raise AffinityError(f"权限不足，无法调整 tid {tid}") from exc
        except OSError as exc:
            raise AffinityError(f"tid {tid}: {exc.strerror or exc}") from exc
    return read_affinity(pid)


def verify_binding(pid: int, expected: int) -> bool:
    """True only if every thread of ``pid`` is bound to exactly ``expected``."""

    affinities = thread_affinities(pid)
    return bool(affinities) and all(cpus == {expected} for cpus in affinities.values())


def process_state(pid: int) -> str:
    return _status_fields(pid).get("State", "?").split(" ", 1)[0]


def process_starttime(pid: int) -> int | None:
    """Kernel start time in clock ticks; guards against PID reuse."""

    try:
        stat = _read(os.path.join(PROC_ROOT, str(pid), "stat"))
    except ProcessGoneError:
        return None
    match = re.search(r"\)\s+\S+\s+(?:\S+\s+){18}(\d+)", stat)
    return int(match.group(1)) if match else None


def is_alive(pid: int) -> bool:
    try:
        return process_state(pid) not in {"Z", "X", "x"}
    except ProcessGoneError:
        return False


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def read_cgroup_cpuset() -> frozenset[int] | None:
    """CPUs allowed by the container's cpuset cgroup, when exposed."""

    paths = [
        "/sys/fs/cgroup/cpuset.cpus.effective",  # cgroup v2
        "/sys/fs/cgroup/cpuset/cpuset.cpus.effective",  # cgroup v1
    ]
    for line in _read_text(os.path.join(PROC_ROOT, "self", "cgroup")).splitlines():
        if ":cpuset:" in line:
            rel = line.split(":", 2)[2].strip("/")
            if rel:
                paths.append(f"/sys/fs/cgroup/cpuset/{rel}/cpuset.cpus")
    for path in paths:
        text = _read_text(path).strip()
        if not text:
            continue
        try:
            return frozenset(parse_cpu_list(text))
        except ValueError:
            continue
    return None


def cpu_pool() -> frozenset[int]:
    """The logical CPUs this container may actually use.

    ``sched_getaffinity`` already reflects taskset and cpuset restrictions of
    the process; intersecting with the visible cpuset keeps the UI honest
    when the cgroup file is readable.
    """

    affinity = frozenset(os.sched_getaffinity(0))
    cpuset = read_cgroup_cpuset()
    return affinity & cpuset if cpuset else affinity


def read_cpu_times() -> dict[int, tuple[int, int]]:
    """``{cpu: (total_ticks, idle_ticks)}`` parsed from /proc/stat."""

    result: dict[int, tuple[int, int]] = {}
    for line in _read_text(os.path.join(PROC_ROOT, "stat")).splitlines():
        if not line.startswith("cpu") or line.startswith("cpu "):
            continue
        fields = line.split()
        try:
            cpu = int(fields[0][3:])
            values = [int(value) for value in fields[1:9]]
        except (ValueError, IndexError):
            continue
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        result[cpu] = (sum(values), idle)
    return result


def cpu_usage_map(sample_s: float = 0.2) -> dict[int, float | None]:
    """Busy percentage for every CPU, from one pair of /proc/stat reads."""

    import time

    first = read_cpu_times()
    time.sleep(sample_s)
    second = read_cpu_times()
    usage: dict[int, float | None] = {}
    for cpu, (total_before, idle_before) in first.items():
        total_after, idle_after = second.get(cpu, (total_before, idle_before))
        total = total_after - total_before
        idle = idle_after - idle_before
        usage[cpu] = round(100.0 * (1.0 - idle / total), 1) if total > 0 else None
    for cpu in second:
        usage.setdefault(cpu, None)
    return usage


def cpu_usage(cpu: int, sample_s: float = 0.2) -> float | None:
    """Busy percentage for one CPU sampled over ``sample_s`` seconds."""

    return cpu_usage_map(sample_s).get(cpu)


def rss_kib(pid: int | str = "self") -> int:
    pid_dir = os.path.join(PROC_ROOT, str(pid))
    match = re.search(r"^Rss:\s+(\d+)\s+kB$", _read_text(os.path.join(pid_dir, "smaps_rollup")), re.MULTILINE)
    if match:
        return int(match.group(1))
    try:
        statm = _read_text(os.path.join(pid_dir, "statm")).split()
        return int(statm[1]) * (os.sysconf("SC_PAGE_SIZE") // 1024)
    except (IndexError, ValueError, OSError):
        return 0
