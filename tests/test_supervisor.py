"""Unit tests for the sample control channel's request/response correlation.

A fake sample process feeds pre-scripted JSON-line answers into the
supervisor's queue, so stale replies, missing ``req_id`` echoes and timeouts
can be exercised deterministically without real processes.
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from unittest import mock

from drill.supervisor import Sample, SampleError


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = _FakeStdin()
        self.poll_result = None

    def poll(self):
        return self.poll_result


class _FakeStdin:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.broken = False

    def write(self, text: str) -> int:
        if self.broken:
            raise BrokenPipeError("closed")
        self.sent.append(text)
        return len(text)

    def flush(self) -> None:
        if self.broken:
            raise BrokenPipeError("closed")


def _make_sample() -> Sample:
    # pid 1 is always alive in this environment's /proc; start_time stays
    # None so the PID-reuse guard is skipped.
    sample = Sample(name="rx-test", pid=1, start_time=None, process=_FakeProcess())
    return sample


def _enqueue(sample: Sample, messages: list[dict | None]) -> None:
    for message in messages:
        sample._lines.put(message)


def _scripted_replier(sample: Sample, script: list[dict], started: threading.Event) -> threading.Thread:
    """Mimic a sample: answer once a request line has been written.

    Messages in ``script`` are put on the queue in order; their ``req_id``
    decides whether the supervisor accepts or skips them.
    """

    def run() -> None:
        started.wait(timeout=2)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not sample.process.stdin.sent:
            time.sleep(0.005)
        for message in script:
            sample._lines.put(message)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


class AskCorrelationTests(unittest.TestCase):
    def test_matching_req_id_reply_is_returned(self):
        sample = _make_sample()
        started = threading.Event()
        thread = _scripted_replier(
            sample, [{"ok": True, "req_id": "aaaa1111bbbb2222", "cpus": "3"}], started
        )
        with mock.patch("drill.supervisor.secrets.token_hex", return_value="aaaa1111bbbb2222"):
            started.set()
            reply = sample.ask("ping", timeout=2)
        thread.join()
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["cpus"], "3")
        sent = json.loads(sample.process.stdin.sent[0])
        self.assertEqual(sent["req_id"], "aaaa1111bbbb2222")
        self.assertEqual(sent["cmd"], "ping")

    def test_stale_reply_with_different_req_id_is_skipped(self):
        sample = _make_sample()
        started = threading.Event()
        thread = _scripted_replier(
            sample,
            [
                {"ok": True, "req_id": "deadbeefdeadbeef", "cpus": "0"},
                {"ok": True, "req_id": "aaaa1111bbbb2222", "cpus": "1"},
            ],
            started,
        )
        with mock.patch("drill.supervisor.secrets.token_hex", return_value="aaaa1111bbbb2222"):
            started.set()
            reply = sample.ask("ping", timeout=2)
        thread.join()
        self.assertEqual(reply["cpus"], "1")
        self.assertEqual(reply["req_id"], "aaaa1111bbbb2222")
        # The stale message was consumed and discarded, not left behind.
        self.assertTrue(sample._lines.empty())

    def test_reply_without_req_id_is_never_accepted(self):
        sample = _make_sample()
        started = threading.Event()
        thread = _scripted_replier(sample, [{"ok": True, "cpus": "2"}], started)
        with mock.patch("drill.supervisor.secrets.token_hex", return_value="aaaa1111bbbb2222"):
            started.set()
            with self.assertRaises(SampleError):
                sample.ask("ping", timeout=0.3)
        thread.join()

    def test_timeout_when_no_reply_arrives(self):
        sample = _make_sample()
        start = time.monotonic()
        with self.assertRaises(SampleError):
            sample.ask("ping", timeout=0.2)
        self.assertLess(time.monotonic() - start, 1.0)

    def test_drain_removes_late_replies_left_by_previous_request(self):
        sample = _make_sample()
        # Leftovers from a request that timed out earlier are queued before
        # this new ask starts and must be drained, not returned.
        _enqueue(
            sample,
            [
                {"ok": True, "req_id": "stale1", "cpus": "0"},
                {"ok": True, "req_id": "stale2", "cpus": "1"},
            ],
        )
        started = threading.Event()
        thread = _scripted_replier(
            sample, [{"ok": True, "req_id": "aaaa1111bbbb2222", "cpus": "4"}], started
        )
        with mock.patch("drill.supervisor.secrets.token_hex", return_value="aaaa1111bbbb2222"):
            started.set()
            reply = sample.ask("ping", timeout=2)
        thread.join()
        self.assertEqual(reply["cpus"], "4")
        self.assertTrue(sample._lines.empty())

    def test_closed_channel_sentinel_is_preserved_by_drain(self):
        sample = _make_sample()
        # A stale answer followed by the channel-closed sentinel: draining the
        # stale traffic must not swallow the sentinel as well.
        sample._lines.put({"ok": True, "req_id": "stale", "cpus": "0"})
        sample._lines.put(None)
        sample._drain_stale()
        self.assertIsNone(sample._lines.get_nowait())

    def test_sentinel_alone_also_closes_the_channel(self):
        sample = _make_sample()
        sample._lines.put(None)
        with self.assertRaises(SampleError):
            sample.ask("ping", timeout=0.2)

    def test_each_request_gets_a_fresh_req_id(self):
        sample = _make_sample()
        ids = iter(["id-0001", "id-0002"])
        stop = threading.Event()

        def replier() -> None:
            # Reply to each request line as it appears, mirroring a real sample.
            seen = 0
            while not stop.wait(0.005):
                lines = sample.process.stdin.sent
                if len(lines) > seen:
                    raw = lines[seen]
                    req_id = json.loads(raw)["req_id"]
                    sample._lines.put({"ok": True, "req_id": req_id})
                    seen += 1
                    if seen == 2:
                        return

        thread = threading.Thread(target=replier, daemon=True)
        thread.start()
        try:
            with mock.patch("drill.supervisor.secrets.token_hex", side_effect=lambda n: next(ids)):
                first = sample.ask("ping", timeout=2)
                second = sample.ask("ping", timeout=2)
        finally:
            stop.set()
        thread.join()
        self.assertEqual(first["req_id"], "id-0001")
        self.assertEqual(second["req_id"], "id-0002")
        sent_ids = [json.loads(raw)["req_id"] for raw in sample.process.stdin.sent]
        self.assertEqual(sent_ids, ["id-0001", "id-0002"])


if __name__ == "__main__":
    unittest.main()
