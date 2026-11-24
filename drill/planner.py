"""Plan affinity moves that never park two sample processes on one core.

A plan is an ordered list of single-sample moves.  After every move the
whole set of samples occupies distinct cores, so no intermediate step ever
co-locates two samples and no process ever has to be suspended to free a
slot.  Cycle requests (the simplest being a swap) borrow a spare core from
the container pool; if no spare core exists the plan is impossible and
:class:`PlanError` is raised before anything is written ("走不通就不动").

Two planning modes:

* :func:`plan_rebind` - normal request.  The live state must already be one
  distinct core per sample.
* :func:`plan_restore` - rollback.  The live state may be arbitrary (a
  partially written step, an outside actor) and is repaired first.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .cpulist import format_cpu_list

_MAX_STEPS = "内部错误：换绑计划未能在有限步内收敛"


class PlanError(ValueError):
    """The requested rebind cannot be expressed as safe single-sample moves."""


def _normalise(values: Mapping[str, Iterable[int] | int]) -> dict[str, frozenset[int]]:
    result: dict[str, frozenset[int]] = {}
    for name, value in values.items():
        cpus = frozenset({value}) if isinstance(value, int) else frozenset(int(cpu) for cpu in value)
        result[name] = cpus
    return result


@dataclass(frozen=True)
class Move:
    sample: str
    source: frozenset[int]
    dest: int
    kind: str  # "direct" | "borrow"
    after: Mapping[str, frozenset[int]] = field(default_factory=dict)

    @property
    def source_text(self) -> str:
        return format_cpu_list(self.source)

    def describe(self) -> str:
        label = "借空闲核周转" if self.kind == "borrow" else "直接换核"
        return f"{self.sample} {self.source_text or '?'} → {self.dest}（{label}）"


def _occupied(state: Mapping[str, frozenset[int]], exclude: str | None = None) -> set[int]:
    cpus: set[int] = set()
    for name, value in state.items():
        if name != exclude:
            cpus.update(value)
    return cpus


def _is_clean(state: Mapping[str, frozenset[int]], target: Mapping[str, int]) -> bool:
    """Every sample alone on exactly its target core."""

    for name, goal in target.items():
        if state[name] != frozenset({goal}):
            return False
    return len(set(target.values())) == len(target)


def _pick_free(free: set[int], order: tuple[int, ...] | None) -> int:
    """Least-loaded spare core when an order is supplied, else lowest id."""

    if order:
        for cpu in order:
            if cpu in free:
                return cpu
    return min(free)


@dataclass(frozen=True)
class Plan:
    initial: Mapping[str, frozenset[int]]
    target: Mapping[str, int]
    pool: frozenset[int]
    moves: tuple[Move, ...]
    borrowed_cores: tuple[int, ...] = ()

    @property
    def steps(self) -> int:
        return len(self.moves)

    @property
    def final_state(self) -> dict[str, int]:
        source = self.moves[-1].after if self.moves else self.initial
        return {name: next(iter(cpus)) for name, cpus in source.items()}

    def summary(self) -> str:
        if not self.moves:
            return "无需换绑"
        return "；".join(move.describe() for move in self.moves)


def _validate_request(
    initial: Mapping[str, frozenset[int]], target: Mapping[str, int], pool: frozenset[int]
) -> None:
    if set(initial) != set(target):
        missing = sorted(set(initial) - set(target))
        extra = sorted(set(target) - set(initial))
        parts = []
        if missing:
            parts.append(f"未给出目标核：{'、'.join(missing)}")
        if extra:
            parts.append(f"未知样例：{'、'.join(extra)}")
        raise PlanError("；".join(parts))
    if len(pool) < len(initial):
        raise PlanError(f"容器可用 CPU 只有 {len(pool)} 颗，少于样例进程数 {len(initial)}")
    wanted = list(target.values())
    if len(set(wanted)) != len(wanted):
        raise PlanError("目标核有重复：各样例进程必须各占一颗不同的核")
    for name, cpu in target.items():
        if cpu not in pool:
            raise PlanError(
                f"{name} 的目标核 {cpu} 不在容器可用 CPU 池 {format_cpu_list(pool)} 内"
            )


def _require_clean_start(initial: Mapping[str, frozenset[int]]) -> None:
    for name, cpus in initial.items():
        if len(cpus) != 1:
            raise PlanError(
                f"{name} 当前绑定为 {format_cpu_list(cpus) or '空'}，不是单核，"
                "拒绝在非单核状态下做常规换绑"
            )
    owners: dict[int, str] = {}
    for name, cpus in initial.items():
        cpu = next(iter(cpus))
        if cpu in owners:
            raise PlanError(f"{name} 与 {owners[cpu]} 当前共用 CPU {cpu}，拒绝作为换绑起点")
        owners[cpu] = name


def _solve(
    initial: Mapping[str, frozenset[int]],
    target: Mapping[str, int],
    pool: frozenset[int],
    *,
    scratch_order: tuple[int, ...] | None,
) -> Plan:
    state = {name: frozenset(cpus) for name, cpus in initial.items()}
    moves: list[Move] = []
    borrowed: list[int] = []
    limit = 6 * len(state) + 12

    while not _is_clean(state, target):
        if len(moves) > limit:
            raise PlanError(_MAX_STEPS)

        pending = sorted(name for name in target if state[name] != frozenset({target[name]}))
        if not pending:
            raise PlanError(_MAX_STEPS)

        chosen: tuple[str, int, str] | None = None
        for name in pending:
            goal = target[name]
            if goal not in _occupied(state, exclude=name):
                chosen = (name, goal, "direct")
                break

        if chosen is None:
            free = pool - _occupied(state)
            if not free:
                detail = "、".join(pending)
                raise PlanError(
                    f"走不通：CPU 池 {format_cpu_list(pool)} 已无空闲核，"
                    f"{detail} 的目标核都还被其他样例占着（互相等待的换核环）。"
                    "未对任何进程做改动。"
                )
            name = pending[0]
            scratch = _pick_free(free, scratch_order)
            borrowed.append(scratch)
            chosen = (name, scratch, "borrow")

        name, dest, kind = chosen
        previous = state[name]
        state = dict(state)
        state[name] = frozenset({dest})
        moves.append(Move(sample=name, source=previous, dest=dest, kind=kind, after=dict(state)))

    return Plan(
        initial=dict(initial),
        target=dict(target),
        pool=pool,
        moves=tuple(moves),
        borrowed_cores=tuple(dict.fromkeys(borrowed)),
    )


def plan_rebind(
    initial: Mapping[str, Iterable[int] | int],
    target: Mapping[str, int],
    pool: Iterable[int],
    *,
    scratch_order: Iterable[int] | None = None,
) -> Plan:
    """Build a safe move list from a clean one-core-per-sample state.

    ``scratch_order`` lists CPUs in the order the caller prefers for
    temporary parking (the executor passes least-busy first); it only
    affects which spare core is borrowed, never the safety invariant.
    """

    init = _normalise(initial)
    pool_set = frozenset(int(cpu) for cpu in pool)
    goals = {name: int(cpu) for name, cpu in target.items()}
    _validate_request(init, goals, pool_set)
    _require_clean_start(init)
    order = tuple(int(cpu) for cpu in scratch_order) if scratch_order is not None else None
    return _solve(init, goals, pool_set, scratch_order=order)


def plan_restore(
    live: Mapping[str, Iterable[int] | int],
    original: Mapping[str, int],
    pool: Iterable[int],
    *,
    scratch_order: Iterable[int] | None = None,
) -> Plan:
    """Build a safe move list back to ``original`` from any live state.

    Already-distinct samples are moved directly; samples that currently
    share a core (possible after a foreign change or a partial write) are
    first parked on free cores.  Every emitted step keeps the invariant
    that no two samples share a core.
    """

    live_state = _normalise(live)
    pool_set = frozenset(int(cpu) for cpu in pool)
    original_goals = {name: int(cpu) for name, cpu in original.items()}
    _validate_request(live_state, original_goals, pool_set)
    order = tuple(int(cpu) for cpu in scratch_order) if scratch_order is not None else None
    return _solve(live_state, original_goals, pool_set, scratch_order=order)
