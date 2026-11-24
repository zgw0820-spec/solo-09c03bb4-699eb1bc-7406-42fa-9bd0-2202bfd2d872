"""Session state: the running samples, the single active rebind job, history.

The page must show what the kernel says, so every listing re-reads
``/proc``.  The orchestrator keeps only two pieces of its own state: which
group is currently accepted (one at a time) and the rollback target captured
from the kernel at the moment the job started.

Fault injection is intentionally limited to "raise an error at step N,
before its affinity writes".  That is a fault on the *submitted step*, not a
way to silently damage a process: the failing step is reported and the job
rolls back, which exercises the real recovery path.
"""

from __future__ import annotations

import itertools
import threading
import time
import traceback
from dataclasses import dataclass, field

from . import kernel
from .cpulist import format_cpu_list
from .planner import PlanError, plan_rebind, plan_restore
from .supervisor import SampleError, SampleSet

MAX_SAMPLES = 4
MIN_SAMPLES = 2


class RequestError(ValueError):
    """Operator input cannot be accepted."""


class BusyError(RuntimeError):
    """Another rebind group is still being executed."""


class StepFailure(RuntimeError):
    """Injected or real failure at a specific step."""


@dataclass(frozen=True)
class StepRecord:
    index: int
    sample: str
    source: str
    dest: int
    kind: str
    status: str  # "ok" | "failed" | "skipped"
    detail: str = ""
    readback: dict[str, str] = field(default_factory=dict)
    at: float = 0.0

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "sample": self.sample,
            "source": self.source,
            "dest": self.dest,
            "kind": self.kind,
            "status": self.status,
            "detail": self.detail,
            "readback": self.readback,
            "at": self.at,
        }


@dataclass(frozen=True)
class JobRecord:
    job_id: int
    requested_at: float
    target: dict[str, int]
    fail_at: int | None
    accepted: bool
    error: str
    plan_summary: str
    steps: tuple[StepRecord, ...]
    rollback_steps: tuple[StepRecord, ...]
    result: str  # "applied" | "rolled_back" | "unchanged" | "noop"
    phase_detail: str
    bindings_after: dict[str, str]

    def as_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "requested_at": self.requested_at,
            "target": self.target,
            "fail_at": self.fail_at,
            "accepted": self.accepted,
            "error": self.error,
            "plan": self.plan_summary,
            "steps": [step.as_dict() for step in self.steps],
            "rollback": [step.as_dict() for step in self.rollback_steps],
            "result": self.result,
            "detail": self.phase_detail,
            "bindings_after": self.bindings_after,
        }


class Session:
    """Owns the sample set and serialises rebind jobs."""

    def __init__(self, sample_count: int = 3, pool: frozenset[int] | None = None) -> None:
        if not MIN_SAMPLES <= sample_count <= MAX_SAMPLES:
            raise RequestError(f"样例进程数必须在 {MIN_SAMPLES}～{MAX_SAMPLES} 之间")
        pool = frozenset(pool) if pool is not None else kernel.cpu_pool()
        if len(pool) < sample_count:
            raise RequestError(
                f"容器可用 CPU {format_cpu_list(pool)} 少于样例数 {sample_count}，无法做到各样例独占一核"
            )
        self.requested_samples = sample_count
        self.pool = pool
        self.set = SampleSet(sample_count, pool)
        self._job_lock = threading.Lock()
        self._executing = False
        self._job_counter = itertools.count(1)
        self.jobs: list[JobRecord] = []
        self.started_at: float | None = None
        self.startup_error: str | None = None

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        if self.started_at is not None:
            return
        try:
            self.set.start()
        except SampleError as exc:
            self.startup_error = str(exc)
            raise
        self.started_at = time.time()

    def shutdown(self) -> None:
        self.set.stop()

    @property
    def busy(self) -> bool:
        return self._executing

    # -- views ---------------------------------------------------------

    def bindings(self) -> dict[str, dict]:
        """Live kernel-reported binding for each sample (single core)."""

        return self.set.read_bindings()

    def cores(self) -> list[dict]:
        """Every CPU in the container pool with its current occupant."""

        live = self.bindings()
        usage = kernel.cpu_usage_map(sample_s=0.1)
        owners: dict[str, str] = {}
        for name, info in live.items():
            if info["cpus"]:
                owners[info["cpus"]] = name
        result = []
        for cpu in sorted(self.pool):
            result.append(
                {
                    "cpu": cpu,
                    "owner": owners.get(str(cpu), "空闲"),
                    "busy": owners.get(str(cpu)) is not None,
                    "usage": usage.get(cpu),
                }
            )
        return result

    def samples_state(self) -> list[dict]:
        live = self.bindings()
        rows = []
        for name in self.set.samples:
            info = live.get(name, {})
            rows.append(
                {
                    "name": name,
                    "pid": info.get("pid"),
                    "state": info.get("state", "?"),
                    "threads": info.get("threads", 0),
                    "cpus": info.get("cpus", ""),
                    "alive": info.get("alive", False),
                    "thread_cpus": info.get("thread_cpus", []),
                }
            )
        return rows

    def current_state(self) -> dict[str, frozenset[int]]:
        return self.set.snapshot_sets()

    def validate_target(self, raw: dict[str, str]) -> dict[str, int]:
        if not isinstance(raw, dict):
            raise RequestError("目标核表单格式不对")
        target: dict[str, int] = {}
        for name, value in raw.items():
            if name not in self.set.samples:
                raise RequestError(f"未知样例：{name}")
            text = str(value).strip()
            if not text:
                raise RequestError(f"{name} 没有填目标核")
            try:
                cpu = int(text)
            except ValueError as exc:
                raise RequestError(f"{name} 的目标核 {text!r} 不是整数") from exc
            target[name] = cpu
        missing = sorted(set(self.set.samples) - set(target))
        if missing:
            raise RequestError(f"缺少目标核：{'、'.join(missing)}")
        for name, cpu in target.items():
            if cpu not in self.pool:
                raise RequestError(
                    f"{name} 的目标核 {cpu} 不在容器可用 CPU 池 {format_cpu_list(self.pool)} 内"
                )
        values = list(target.values())
        if len(set(values)) != len(values):
            raise RequestError("目标核有重复：各样例进程必须各占一颗不同的核")
        return target

    def parse_fail_at(self, raw: str | int | None) -> int | None:
        if raw is None or str(raw).strip() == "":
            return None
        text = str(raw).strip()
        try:
            value = int(text)
        except ValueError as exc:
            raise RequestError(f"故障步号 {text!r} 不是整数") from exc
        if value < 1:
            raise RequestError("故障步号必须 ≥ 1")
        return value

    # -- execution -----------------------------------------------------

    @property
    def executing(self) -> bool:
        return self._executing

    def submit(self, raw_target: dict[str, str], fail_at: int | None = None) -> JobRecord:
        """Run one rebind group synchronously and return its record.

        Only one group is accepted at a time; a concurrent submission gets
        :class:`BusyError` and changes nothing.
        """

        if not self._job_lock.acquire(blocking=False):
            raise BusyError("上一组换绑还在执行，本次提交未受理")
        try:
            self._executing = True
            return self._run_job(raw_target, fail_at)
        finally:
            self._executing = False
            self._job_lock.release()

    def _job_id(self) -> int:
        return next(self._job_counter)

    def _readback(self) -> dict[str, str]:
        live = self.bindings()
        return {name: info["cpus"] for name, info in live.items()}

    def _run_job(self, raw_target: dict[str, str], fail_at: int | None) -> JobRecord:
        job_id = self._job_id()
        requested_at = time.time()
        try:
            target = self.validate_target(raw_target)
        except RequestError as exc:
            record = JobRecord(
                job_id=job_id,
                requested_at=requested_at,
                target={},
                fail_at=fail_at,
                accepted=False,
                error=str(exc),
                plan_summary="",
                steps=(),
                rollback_steps=(),
                result="unchanged",
                phase_detail="请求未受理，未做任何改动",
                bindings_after=self._readback(),
            )
            self.jobs.append(record)
            return record

        try:
            live = self.current_state()
        except SampleError as exc:
            record = JobRecord(
                job_id=job_id,
                requested_at=requested_at,
                target=target,
                fail_at=fail_at,
                accepted=False,
                error=str(exc),
                plan_summary="",
                steps=(),
                rollback_steps=(),
                result="unchanged",
                phase_detail="读取当前绑定失败，未做任何改动",
                bindings_after={},
            )
            self.jobs.append(record)
            return record

        try:
            plan = plan_rebind(live, target, self.pool, scratch_order=self._scratch_order())
        except PlanError as exc:
            record = JobRecord(
                job_id=job_id,
                requested_at=requested_at,
                target=target,
                fail_at=fail_at,
                accepted=True,
                error=str(exc),
                plan_summary="",
                steps=(),
                rollback_steps=(),
                result="unchanged",
                phase_detail="计划阶段拒绝执行，未做任何改动",
                bindings_after=self._readback(),
            )
            self.jobs.append(record)
            return record

        if plan.steps == 0:
            record = JobRecord(
                job_id=job_id,
                requested_at=requested_at,
                target=target,
                fail_at=fail_at,
                accepted=True,
                error="",
                plan_summary="目标与当前一致",
                steps=(),
                rollback_steps=(),
                result="noop",
                phase_detail="无需换绑",
                bindings_after=self._readback(),
            )
            self.jobs.append(record)
            return record

        if fail_at is not None and fail_at > plan.steps:
            record = JobRecord(
                job_id=job_id,
                requested_at=requested_at,
                target=target,
                fail_at=fail_at,
                accepted=False,
                error=f"故障步号 {fail_at} 超出本组步数 {plan.steps}，未受理",
                plan_summary=plan.summary(),
                steps=(),
                rollback_steps=(),
                result="unchanged",
                phase_detail="请求未受理，未做任何改动",
                bindings_after=self._readback(),
            )
            self.jobs.append(record)
            return record

        original = {name: next(iter(cpus)) for name, cpus in live.items()}
        steps: list[StepRecord] = []
        failure: str | None = None

        for index, move in enumerate(plan.moves, start=1):
            if fail_at == index:
                failure = f"注入故障：第 {index} 步在设置亲和性之前报错"
                steps.append(
                    StepRecord(
                        index=index,
                        sample=move.sample,
                        source=move.source_text,
                        dest=move.dest,
                        kind=move.kind,
                        status="failed",
                        detail=failure,
                        readback=self._readback(),
                        at=time.time(),
                    )
                )
                break
            try:
                guard = self._guard_destination(move.sample, move.dest)
                if guard:
                    raise StepFailure(guard)
                kernel.set_affinity(self.set.samples[move.sample].pid, {move.dest})
                readback = self._readback()
                ok = readback.get(move.sample) == str(move.dest)
                steps.append(
                    StepRecord(
                        index=index,
                        sample=move.sample,
                        source=move.source_text,
                        dest=move.dest,
                        kind=move.kind,
                        status="ok" if ok else "failed",
                        detail="" if ok else f"内核回读为 {readback.get(move.sample)!r}",
                        readback=readback,
                        at=time.time(),
                    )
                )
                if not ok:
                    failure = f"第 {index} 步设置后内核回读不一致"
                    break
                violation = self._violation()
                if violation:
                    failure = f"第 {index} 步后出现共核：{violation}"
                    break
            except (StepFailure, kernel.ProcessGoneError, kernel.AffinityError, SampleError) as exc:
                failure = str(exc)
                steps.append(
                    StepRecord(
                        index=index,
                        sample=move.sample,
                        source=move.source_text,
                        dest=move.dest,
                        kind=move.kind,
                        status="failed",
                        detail=failure,
                        readback=self._readback(),
                        at=time.time(),
                    )
                )
                break

        if failure is None:
            record = JobRecord(
                job_id=job_id,
                requested_at=requested_at,
                target=target,
                fail_at=fail_at,
                accepted=True,
                error="",
                plan_summary=plan.summary(),
                steps=tuple(steps),
                rollback_steps=(),
                result="applied",
                phase_detail="全部步骤完成，内核回读一致",
                bindings_after=self._readback(),
            )
            self.jobs.append(record)
            return record

        rollback, detail = self._rollback(original, failure)
        record = JobRecord(
            job_id=job_id,
            requested_at=requested_at,
            target=target,
            fail_at=fail_at,
            accepted=True,
            error=failure,
            plan_summary=plan.summary(),
            steps=tuple(steps),
            rollback_steps=tuple(rollback),
            result="rolled_back" if all(s.status == "ok" for s in rollback) else "rollback_failed",
            phase_detail=detail,
            bindings_after=self._readback(),
        )
        self.jobs.append(record)
        return record

    def _rollback(self, original: dict[str, int], failure: str) -> tuple[list[StepRecord], str]:
        """Return every sample to ``original`` without ever sharing a core."""

        steps: list[StepRecord] = []
        try:
            live = self.current_state()
            plan = plan_restore(live, original, self.pool, scratch_order=self._scratch_order())
        except (PlanError, SampleError, kernel.ProcessGoneError, kernel.AffinityError) as exc:
            return steps, f"回退计划失败：{exc}。样例可能仍停在异常绑定，请人工核对"

        for index, move in enumerate(plan.moves, start=1):
            try:
                kernel.set_affinity(self.set.samples[move.sample].pid, {move.dest})
                readback = self._readback()
                ok = readback.get(move.sample) == str(move.dest)
                steps.append(
                    StepRecord(
                        index=index,
                        sample=move.sample,
                        source=move.source_text,
                        dest=move.dest,
                        kind=move.kind,
                        status="ok" if ok else "failed",
                        detail="" if ok else f"内核回读为 {readback.get(move.sample)!r}",
                        readback=readback,
                        at=time.time(),
                    )
                )
                if not ok:
                    return steps, f"{failure}；回退第 {index} 步回读不一致，已停止"
                violation = self._violation()
                if violation:
                    return steps, f"{failure}；回退第 {index} 步后出现共核：{violation}"
            except (kernel.ProcessGoneError, kernel.AffinityError, SampleError) as exc:
                steps.append(
                    StepRecord(
                        index=index,
                        sample=move.sample,
                        source=move.source_text,
                        dest=move.dest,
                        kind=move.kind,
                        status="failed",
                        detail=str(exc),
                        readback=self._readback(),
                        at=time.time(),
                    )
                )
                return steps, f"{failure}；回退中断：{exc}"

        restored = self._readback()
        expected = {name: str(cpu) for name, cpu in original.items()}
        if restored != expected:
            return steps, f"{failure}；回退后绑定与原始绑定不一致：{restored} != {expected}"
        return steps, f"{failure}；已按原绑定恢复，内核回读一致"

    def _guard_destination(self, sample: str, dest: int) -> str:
        """Refuse a write whose destination is currently held by someone else."""

        live = self.current_state()
        holders = [name for name, cpus in live.items() if name != sample and dest in cpus]
        if holders:
            return f"目标核 {dest} 仍被 {'、'.join(sorted(holders))} 占用，拒绝共核写入"
        return ""

    def _violation(self) -> str:
        live = self.current_state()
        owners: dict[int, str] = {}
        for name, cpus in live.items():
            if len(cpus) != 1:
                return f"{name} 不是单核绑定（{format_cpu_list(cpus) or '空'}）"
            cpu = next(iter(cpus))
            if cpu in owners:
                return f"{name} 与 {owners[cpu]} 共用 CPU {cpu}"
            owners[cpu] = name
        return ""

    def _scratch_order(self) -> tuple[int, ...]:
        """Prefer the least busy spare core when parking a process."""

        usage = kernel.cpu_usage_map(sample_s=0.05)
        return tuple(
            sorted(self.pool, key=lambda cpu: (usage.get(cpu) is None, usage.get(cpu) or 0.0, cpu))
        )


def describe_exception(exc: BaseException) -> str:
    return "".join(traceback.format_exception_only(type(exc), exc)).strip()
