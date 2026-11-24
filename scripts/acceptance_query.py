#!/usr/bin/env python3
"""HTTP-level checks for the sample query entry ("查询样例").

Drives the running drill page exactly like the on-call engineer and verifies
the three fixes for the query path:

1. a quiet query returns two readings (sample self-report + orchestrator
   kernel read) that agree, with a request id on the self-report;
2. a query that overlaps a rebind (or its rollback) is answered 409
   "请重查" instead of mixing the old and new core numbers as success;
3. a timed-out query keeps its timeout prompt; after the sample recovers the
   next query succeeds and never returns the late reply of the timed-out one.

    python3 scripts/acceptance_query.py
    BASE=http://127.0.0.1:9000 python3 scripts/acceptance_query.py
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
import urllib.error
import urllib.request

BASE = os.environ.get("BASE", "http://127.0.0.1:8008").rstrip("/")


def http_get(path: str):
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def state() -> dict:
    _, body = http_get("/api/state")
    return body


def bindings() -> dict[str, str]:
    return {row["name"]: row["cpus"] for row in state()["samples"]}


def pids() -> dict[str, int]:
    return {row["name"]: row["pid"] for row in state()["samples"]}


def submit(target: dict[str, str], fail_at: int | None = None) -> dict:
    import http.cookiejar
    import re
    import urllib.parse

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    page = opener.open(BASE + "/", timeout=10).read().decode()
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    fields = {"csrf": csrf, "fail_at": "" if fail_at is None else str(fail_at)}
    for name, cpu in target.items():
        fields[f"target_{name}"] = str(cpu)
    data = urllib.parse.urlencode(fields).encode()
    opener.open(urllib.request.Request(BASE + "/rebind", data=data, method="POST"), timeout=30).read()
    return state()["jobs"][-1]


def check(condition: bool, message: str, failures: list[str]) -> None:
    print(("  OK   " if condition else "  FAIL ") + message)
    if not condition:
        failures.append(message)


def main() -> int:
    snapshot = state()
    names = sorted(row["name"] for row in snapshot["samples"])
    if len(names) < 2:
        print("样例不足 2 个，无法演练")
        return 1
    failures: list[str] = []
    print(f"页面 {BASE}，CPU 池 {snapshot['pool']}，样例 {names}")

    # -- 1. 安静期查询：两份读数一致，且带请求号 -----------------------
    print("场景一：安静期点“查询样例”")
    status, body = http_get(f"/api/samples/{names[0]}")
    check(status == 200, f"HTTP 200（实际 {status}: {body.get('error')}）", failures)
    check(bool(body.get("req_id")), "样例自报带本次请求号 req_id", failures)
    check(body.get("consistent") is True, "样例自报与编排器内核读数一致", failures)
    check(body["cpus"] == body["orchestrator_view"]["cpus"], "两份读数核号相同", failures)
    check(body["cpus"] == bindings()[names[0]], "读数与 /api/state 内核视图一致", failures)
    print(f"  核号 {body['cpus']}，req_id={body['req_id']}")

    if not hasattr(signal, "SIGSTOP"):
        print("平台不支持 SIGSTOP，跳过超时/并发场景")
        return 1 if failures else 0

    pid_by_name = pids()

    # -- 2. 查询期间换绑：等待回信时提交换绑，应只提示重查 -------------
    print("场景二：等待样例回信期间提交换绑")
    target = dict(bindings())
    target[names[0]], target[names[1]] = bindings()[names[1]], bindings()[names[0]]
    os.kill(pid_by_name[names[0]], signal.SIGSTOP)
    holder: dict = {}
    try:
        thread = threading.Thread(target=lambda: holder.update(zip(("status", "body"), http_get(f"/api/samples/{names[0]}"))))
        thread.start()
        time.sleep(0.4)  # let the query enter the sample-answer wait
        job = submit(target)
    finally:
        os.kill(pid_by_name[names[0]], signal.SIGCONT)
    thread.join(timeout=10)
    check(job["result"] == "applied", f"换绑照常完成（实际 {job['result']}）", failures)
    check(holder.get("status") == 409, f"查询回 409 请重查（实际 {holder.get('status')}）", failures)
    check(bool(holder.get("body", {}).get("retry")), "响应带 retry 标记", failures)
    check("重查" in holder.get("body", {}).get("error", ""), "错误信息提示重查", failures)
    check(bindings() == target, "换绑未被查询阻塞，最终绑定即目标", failures)

    # 恢复后直接再查：迟到回信不得顶替本次结果
    deadline = time.monotonic() + 8
    recovered = False
    while time.monotonic() < deadline:
        status, body = http_get(f"/api/samples/{names[0]}")
        if status == 200:
            recovered = True
            break
        time.sleep(0.3)
    check(recovered, "样例恢复后直接再查成功", failures)
    if recovered:
        check(body["consistent"] and body["cpus"] == target[names[0]], "再查核号为换绑后的新核且两读数一致", failures)

    # -- 3. 查询期间回退（含回到原核）：同样只提示重查 -----------------
    print("场景三：等待样例回信期间提交会回退的换绑")
    original = bindings()
    order = names[:3] if len(names) >= 3 else names[:2]
    target3 = dict(original)
    target3.update({order[i]: original[order[(i + 1) % len(order)]] for i in range(len(order))})
    stalled = order[0]
    os.kill(pid_by_name[stalled], signal.SIGSTOP)
    holder.clear()
    try:
        thread = threading.Thread(target=lambda: holder.update(zip(("status", "body"), http_get(f"/api/samples/{stalled}"))))
        thread.start()
        time.sleep(0.4)
        job = submit(target3, fail_at=2)
    finally:
        os.kill(pid_by_name[stalled], signal.SIGCONT)
    thread.join(timeout=10)
    check(job["result"] == "rolled_back", f"任务回退（实际 {job['result']}）", failures)
    check(holder.get("status") == 409, f"查询回 409 请重查（实际 {holder.get('status')}）", failures)
    check("重查" in holder.get("body", {}).get("error", ""), "回退窗口的查询也提示重查", failures)
    check(bindings() == original, "回退后绑定与原核一致", failures)
    deadline = time.monotonic() + 8
    ok = False
    while time.monotonic() < deadline:
        status, body = http_get(f"/api/samples/{stalled}")
        if status == 200:
            ok = True
            break
        time.sleep(0.3)
    check(ok and body["consistent"] and body["cpus"] == original[stalled], "恢复后再查确认回到原核", failures)

    # -- 4. 纯超时（无换绑）：保留超时提示，恢复后可查 -----------------
    print("场景四：样例停住导致查询超时，之后恢复")
    os.kill(pid_by_name[names[0]], signal.SIGSTOP)
    try:
        start = time.monotonic()
        status, body = http_get(f"/api/samples/{names[0]}")
        elapsed = time.monotonic() - start
    finally:
        os.kill(pid_by_name[names[0]], signal.SIGCONT)
    check(status == 503, f"超时回 503（实际 {status}）", failures)
    check("超时" in body.get("error", ""), "保留超时提示文案", failures)
    check(elapsed < 4.0, f"约 2s 超时返回（实际 {elapsed:.1f}s）", failures)
    deadline = time.monotonic() + 8
    ok = False
    while time.monotonic() < deadline:
        status, body = http_get(f"/api/samples/{names[0]}")
        if status == 200:
            ok = True
            break
        time.sleep(0.3)
    check(ok and body["consistent"], "样例恢复后可直接再查成功", failures)

    print()
    if failures:
        print(f"有 {len(failures)} 项未通过")
        return 1
    print("查询入口各场景全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
