"""Unit tests for the planner and CPU-list helpers (no processes started)."""

from __future__ import annotations

import itertools
import random
import unittest

from drill.cpulist import CpuListError, format_cpu_list, parse_cpu_list
from drill.planner import PlanError, plan_rebind, plan_restore


class CpuListTests(unittest.TestCase):
    def test_parse_and_format_roundtrip(self):
        cases = [
            "0",
            "0-3",
            "0-3,6",
            "0,2,4-6,9",
            " 1 , 3-4 ",
        ]
        for text in cases:
            parsed = parse_cpu_list(text)
            self.assertEqual(parse_cpu_list(format_cpu_list(parsed)), parsed)

    def test_format_groups_consecutive(self):
        self.assertEqual(format_cpu_list({3, 1, 2, 7}), "1-3,7")
        self.assertEqual(format_cpu_list({4}), "4")
        self.assertEqual(format_cpu_list(set()), "")

    def test_empty_is_empty_set(self):
        self.assertEqual(parse_cpu_list(""), set())
        self.assertEqual(parse_cpu_list("   "), set())

    def test_rejects_garbage(self):
        for text in ["1-", "-2", "3-1", "a", "1,,2", "1-2-3", "five"]:
            with self.assertRaises(CpuListError, msg=text):
                parse_cpu_list(text)


def _assert_safe_plan(test: unittest.TestCase, plan) -> None:
    """Every step's after-state keeps one distinct core per sample."""

    for move in plan.moves:
        seen: dict[int, str] = {}
        for name, cpus in move.after.items():
            test.assertEqual(len(cpus), 1, f"{name} 在 {move.sample} 的步骤后不是单核")
            cpu = next(iter(cpus))
            test.assertNotIn(cpu, seen, f"{move.sample} 的步骤后 {name} 与 {seen.get(cpu)} 共核")
            seen[cpu] = name
        test.assertIn(move.dest, plan.pool)
    if plan.moves:
        test.assertEqual(plan.final_state, dict(plan.target))


class PlannerTests(unittest.TestCase):
    def test_swap_borrows_spare_core(self):
        plan = plan_rebind({"a": 0, "b": 1}, {"a": 1, "b": 0}, {0, 1, 2})
        self.assertEqual(plan.final_state, {"a": 1, "b": 0})
        self.assertEqual(plan.borrowed_cores, (2,))
        self.assertIn("borrow", [move.kind for move in plan.moves])
        _assert_safe_plan(self, plan)

    def test_swap_without_spare_core_is_refused(self):
        with self.assertRaises(PlanError) as ctx:
            plan_rebind({"a": 0, "b": 1}, {"a": 1, "b": 0}, {0, 1})
        self.assertIn("走不通", str(ctx.exception))

    def test_three_way_cycle_with_spare(self):
        plan = plan_rebind({"a": 0, "b": 1, "c": 2}, {"a": 1, "b": 2, "c": 0}, {0, 1, 2, 3})
        self.assertEqual(plan.final_state, {"a": 1, "b": 2, "c": 0})
        _assert_safe_plan(self, plan)

    def test_three_way_cycle_without_spare_is_refused(self):
        with self.assertRaises(PlanError):
            plan_rebind({"a": 0, "b": 1, "c": 2}, {"a": 1, "b": 2, "c": 0}, {0, 1, 2})

    def test_direct_moves_need_no_spare(self):
        plan = plan_rebind({"a": 0, "b": 1}, {"a": 0, "b": 2}, {0, 1, 2})
        self.assertEqual([move.kind for move in plan.moves], ["direct"])
        self.assertEqual(plan.borrowed_cores, ())
        _assert_safe_plan(self, plan)

    def test_noop_when_already_there(self):
        plan = plan_rebind({"a": 0, "b": 1}, {"a": 0, "b": 1}, {0, 1})
        self.assertEqual(plan.steps, 0)
        self.assertEqual(plan.summary(), "无需换绑")

    def test_rejects_duplicate_targets(self):
        with self.assertRaises(PlanError):
            plan_rebind({"a": 0, "b": 1}, {"a": 1, "b": 1}, {0, 1, 2})

    def test_rejects_target_outside_pool(self):
        with self.assertRaises(PlanError):
            plan_rebind({"a": 0, "b": 1}, {"a": 3, "b": 1}, {0, 1, 2})

    def test_rejects_missing_sample(self):
        with self.assertRaises(PlanError):
            plan_rebind({"a": 0, "b": 1}, {"a": 0}, {0, 1, 2})

    def test_rejects_non_single_core_start(self):
        with self.assertRaises(PlanError):
            plan_rebind({"a": {0, 1}, "b": 2}, {"a": 0, "b": 1}, {0, 1, 2})

    def test_rejects_shared_start(self):
        with self.assertRaises(PlanError):
            plan_rebind({"a": 0, "b": 0}, {"a": 0, "b": 1}, {0, 1, 2})

    def test_scratch_order_chooses_spare(self):
        plan = plan_rebind({"a": 0, "b": 1}, {"a": 1, "b": 0}, {0, 1, 2, 3}, scratch_order=(3, 2))
        self.assertEqual(plan.borrowed_cores, (3,))
        _assert_safe_plan(self, plan)

    def test_random_permutations_are_always_safe_or_refused(self):
        rng = random.Random(20261124)
        for size in (2, 3, 4):
            pool = frozenset(range(6))
            names = [f"p{i}" for i in range(size)]
            for _ in range(60):
                start = rng.sample(sorted(pool), size)
                goal = rng.sample(sorted(pool), size)
                initial = dict(zip(names, start))
                target = dict(zip(names, goal))
                plan = plan_rebind(initial, target, pool)
                _assert_safe_plan(self, plan)
                self.assertEqual(plan.final_state, target)

    def test_random_permutations_without_spare_are_refused_or_safe(self):
        rng = random.Random(7)
        for size in (2, 3, 4):
            pool = frozenset(range(size))
            names = [f"p{i}" for i in range(size)]
            for _ in range(30):
                start = rng.sample(sorted(pool), size)
                goal = rng.sample(sorted(pool), size)
                initial = dict(zip(names, start))
                target = dict(zip(names, goal))
                try:
                    plan = plan_rebind(initial, target, pool)
                except PlanError:
                    # A non-identity permutation with no spare core always
                    # contains a cycle and must be refused.
                    self.assertNotEqual(initial, target)
                    continue
                _assert_safe_plan(self, plan)


class RestoreTests(unittest.TestCase):
    def test_restore_from_clean_state(self):
        plan = plan_restore({"a": 2, "b": 0, "c": 1}, {"a": 0, "b": 1, "c": 2}, {0, 1, 2, 3})
        self.assertEqual(plan.final_state, {"a": 0, "b": 1, "c": 2})
        _assert_safe_plan(self, plan)

    def test_restore_repairs_shared_core_without_leaving_it_shared(self):
        # a and b both sit on CPU 0 (e.g. after an outside actor); restoring
        # must first park one of them and never leave them sharing a core.
        plan = plan_restore({"a": 0, "b": 0, "c": 2}, {"a": 0, "b": 1, "c": 2}, {0, 1, 2, 3})
        self.assertEqual(plan.final_state, {"a": 0, "b": 1, "c": 2})
        for move in plan.moves:
            seen: dict[int, str] = {}
            for name, cpus in move.after.items():
                self.assertEqual(len(cpus), 1)
                cpu = next(iter(cpus))
                self.assertNotIn(cpu, seen)
                seen[cpu] = name

    def test_restore_refused_when_pool_full_and_cycle(self):
        # Rollback may not fall back to co-locating processes either: a swap
        # inside a full pool has no safe intermediate step.
        with self.assertRaises(PlanError):
            plan_restore({"a": 0, "b": 1}, {"a": 1, "b": 0}, {0, 1})

    def test_restore_fixes_shared_core_even_in_full_pool(self):
        plan = plan_restore({"a": 0, "b": 0}, {"a": 0, "b": 1}, {0, 1})
        self.assertEqual(plan.final_state, {"a": 0, "b": 1})
        self.assertEqual([(m.sample, m.dest) for m in plan.moves], [("b", 1)])


class ExhaustiveSmallTests(unittest.TestCase):
    def test_every_permutation_with_one_spare_is_planned_safely(self):
        pool = frozenset(range(4))
        names = ["a", "b", "c"]
        for start in itertools.permutations(sorted(pool), 3):
            for goal in itertools.permutations(sorted(pool), 3):
                plan = plan_rebind(dict(zip(names, start)), dict(zip(names, goal)), pool)
                _assert_safe_plan(self, plan)
                self.assertEqual(plan.final_state, dict(zip(names, goal)))


if __name__ == "__main__":
    unittest.main()
