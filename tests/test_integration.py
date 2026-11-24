"""Integration tests against real sample processes and real affinity writes.

These tests start actual OS processes and use ``sched_setaffinity``.  They
skip automatically when the environment cannot provide a usable CPU pool.
"""

from __future__ import annotations

import os
import threading
import time
import unittest

from drill import kernel
from drill.session import BusyError, Session
from drill.web import create_app


def _pool(size: int) -> frozenset[int] | None:
    available = sorted(kernel.cpu_pool())
    if len(available) < size + 1:
        return None
    # Leave one spare core so cycle plans are possible.
    return frozenset(available[: size + 1])


def _safe(bindings: dict[str, str]) -> bool:
    values = list(bindings.values())
    return (
        all(value and "," not in value and "-" not in value for value in values)
        and len(set(values)) == len(values)
    )


class IntegrationBase(unittest.TestCase):
    samples = 3

    def setUp(self):
        pool = _pool(self.samples)
        if pool is None:
            self.skipTest("CPU 池不足（需要至少 4 颗可用 CPU）")
        try:
            self.session = Session(sample_count=self.samples, pool=pool)
            self.session.start()
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"无法启动样例进程：{exc}")
        self.addCleanup(self.session.shutdown)

    def bindings(self) -> dict[str, str]:
        return {name: info["cpus"] for name, info in self.session.bindings().items()}

    def kernel_bindings(self) -> dict[str, str]:
        return {
            name: kernel.read_affinity_text(sample.pid, sample.pid)
            for name, sample in self.session.set.samples.items()
        }

    def test_samples_are_single_threaded_single_core_distinct(self):
        rows = self.session.samples_state()
        self.assertEqual(len(rows), self.samples)
        for row in rows:
            self.assertEqual(row["threads"], 1, row)
            self.assertTrue(row["alive"], row)
            self.assertNotIn(",", row["cpus"])
            self.assertNotIn("-", row["cpus"])
        self.assertEqual(len({row["cpus"] for row in rows}), self.samples)
        # The page readback and the direct /proc read must agree.
        self.assertEqual(self.bindings(), self.kernel_bindings())

    def test_swap_is_applied_and_every_step_is_safe(self):
        before = self.bindings()
        names = sorted(before)
        target = dict(before)
        target[names[0]], target[names[1]] = before[names[1]], before[names[0]]
        job = self.session.submit(target)
        self.assertEqual(job.result, "applied", job.error)
        self.assertEqual(self.bindings(), target)
        self.assertEqual(self.kernel_bindings(), target)
        self.assertTrue(any(step.kind == "borrow" for step in job.steps))
        for step in job.steps:
            self.assertEqual(step.status, "ok")
            self.assertTrue(_safe(step.readback), step.readback)
            self.assertEqual(step.readback[step.sample], str(step.dest))
        self.assertEqual(job.bindings_after, target)

    def test_injected_failure_at_each_step_restores_original(self):
        original = self.bindings()
        names = sorted(original)
        # A 3-cycle between the samples, so the plan has several steps.
        target = dict(original)
        for index, name in enumerate(names):
            target[name] = original[names[(index + 1) % len(names)]]
        for fail_at in range(1, 8):
            with self.subTest(fail_at=fail_at):
                # Reset to the original bindings before each attempt.
                reset = self.session.submit(original)
                self.assertIn(reset.result, {"applied", "noop"}, reset.error)
                job = self.session.submit(target, fail_at=fail_at)
                if fail_at > len(job.steps) and job.result == "unchanged":
                    break  # step number beyond this plan's length: refused
                self.assertEqual(job.result, "rolled_back", job.error)
                self.assertEqual(self.bindings(), original)
                self.assertEqual(self.kernel_bindings(), original)
                self.assertTrue(_safe(job.bindings_after))
                for step in job.steps:
                    self.assertTrue(_safe(step.readback), step.readback)
                for step in job.rollback_steps:
                    self.assertEqual(step.status, "ok")
                    self.assertTrue(_safe(step.readback), step.readback)

    def test_page_lists_kernel_readback_per_step(self):
        app = create_app(self.session)
        client = app.test_client()
        before = self.bindings()
        names = sorted(before)
        target = dict(before)
        target[names[0]], target[names[1]] = before[names[1]], before[names[0]]
        state_before = client.get("/api/state").get_json()
        self.assertEqual(
            {row["name"]: row["cpus"] for row in state_before["samples"]}, before
        )
        self.session.submit(target)
        page = client.get("/").data.decode()
        state = client.get("/api/state").get_json()
        job = state["jobs"][-1]
        self.assertEqual(job["result"], "applied")
        for step in job["steps"]:
            for name, cpus in step["readback"].items():
                self.assertIn(f"{name}={cpus}", page)
        self.assertIn("每一步的内核回读", page)
        self.assertIn("容器可用逻辑 CPU", page)
        first = names[0]
        detail = client.get(f"/api/samples/{first}").get_json()
        self.assertTrue(detail["ok"], detail)
        self.assertEqual(detail["threads"], 1)
        self.assertEqual(detail["cpus"], target[first])
        self.assertEqual(detail["orchestrator_view"]["cpus"], target[first])

    def test_noop_when_target_matches(self):
        job = self.session.submit(self.bindings())
        self.assertEqual(job.result, "noop")

    def test_duplicate_target_is_refused_without_changes(self):
        before = self.bindings()
        names = sorted(before)
        target = dict(before)
        target[names[1]] = before[names[0]]
        job = self.session.submit(target)
        self.assertEqual(job.result, "unchanged")
        self.assertIn("重复", job.error)
        self.assertEqual(self.bindings(), before)

    def test_busy_submission_is_rejected(self):
        acquired = self.session._job_lock.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            with self.assertRaises(BusyError):
                self.session.submit({name: cpus for name, cpus in self.bindings().items()})
        finally:
            self.session._job_lock.release()
        self.assertEqual(self.bindings(), self.kernel_bindings())


class NoSpareCoreTests(unittest.TestCase):
    def test_swap_refused_when_pool_has_no_spare(self):
        pool = _pool(3)
        if pool is None:
            self.skipTest("CPU 不足")
        tight = frozenset(sorted(pool)[:3])
        session = Session(sample_count=3, pool=tight)
        try:
            session.start()
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"无法启动样例进程：{exc}")
        self.addCleanup(session.shutdown)
        before = {name: info["cpus"] for name, info in session.bindings().items()}
        names = sorted(before)
        target = dict(before)
        target[names[0]], target[names[1]] = before[names[1]], before[names[0]]
        job = session.submit(target)
        self.assertEqual(job.result, "unchanged")
        self.assertIn("走不通", job.error)
        after = {name: info["cpus"] for name, info in session.bindings().items()}
        self.assertEqual(after, before)


class ConcurrentSubmissionTests(unittest.TestCase):
    def test_two_simultaneous_submissions_are_serialised_or_rejected(self):
        pool = _pool(2)
        if pool is None:
            self.skipTest("CPU 不足")
        session = Session(sample_count=2, pool=pool)
        try:
            session.start()
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"无法启动样例进程：{exc}")
        self.addCleanup(session.shutdown)
        before = {name: info["cpus"] for name, info in session.bindings().items()}
        names = sorted(before)
        target = dict(before)
        target[names[0]], target[names[1]] = before[names[1]], before[names[0]]
        results: list[str] = []
        lock = threading.Lock()

        def attempt():
            try:
                job = session.submit(target)
                outcome = job.result
            except BusyError:
                outcome = "busy"
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        after = {name: info["cpus"] for name, info in session.bindings().items()}
        # Whether both ran back to back or one was rejected, the final state
        # must be a clean single-core mapping.
        self.assertTrue(_safe(after), after)
        self.assertIn("applied", results)


if __name__ == "__main__":
    unittest.main()
