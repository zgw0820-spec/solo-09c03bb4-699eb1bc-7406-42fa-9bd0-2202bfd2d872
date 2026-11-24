#!/usr/bin/env python3
"""Drive a running drill page over HTTP: swap and injected-failure scenarios.

The script only uses the public page and its JSON state endpoint, so it
exercises exactly what the on-call engineer would click.

    python3 scripts/acceptance.py                    # http://127.0.0.1:8008
    BASE=http://127.0.0.1:9000 python3 scripts/acceptance.py
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import re
import sys
import urllib.parse
import urllib.request

BASE = os.environ.get("BASE", "http://127.0.0.1:8008").rstrip("/")


class Client:
    def __init__(self, base: str) -> None:
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def get(self, path: str) -> str:
        with self.opener.open(self.base + path, timeout=10) as response:
            return response.read().decode()

    def post(self, path: str, fields: dict[str, str]) -> str:
        data = urllib.parse.urlencode(fields).encode()
        request = urllib.request.Request(self.base + path, data=data, method="POST")
        with self.opener.open(request, timeout=30) as response:
            return response.read().decode()

    def state(self) -> dict:
        return json.loads(self.get("/api/state"))

    def bindings(self) -> dict[str, str]:
        return {row["name"]: row["cpus"] for row in self.state()["samples"]}

    def submit(self, target: dict[str, str], fail_at: int | None = None) -> dict:
        page = self.get("/")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page)
        if not csrf:
            raise SystemExit("页面没有 CSRF 令牌")
        fields = {"csrf": csrf.group(1), "fail_at": "" if fail_at is None else str(fail_at)}
        for name, cpu in target.items():
            fields[f"target_{name}"] = str(cpu)
        before = len(self.state()["jobs"])
        self.post("/rebind", fields)
        jobs = self.state()["jobs"]
        if len(jobs) == before:
            raise SystemExit("提交后页面没有出现新的执行记录")
        return jobs[-1]


def check(condition: bool, message: str, failures: list[str]) -> None:
    print(("  OK   " if condition else "  FAIL ") + message)
    if not condition:
        failures.append(message)


def single_and_distinct(bindings: dict[str, str]) -> bool:
    values = list(bindings.values())
    return all(value and "," not in value and "-" not in value for value in values) and len(set(values)) == len(values)


def main() -> int:
    client = Client(BASE)
    state = client.state()
    names = sorted(row["name"] for row in state["samples"])
    if len(names) < 2:
        print("样例不足 2 个，无法演练互换")
        return 1
    failures: list[str] = []
    print(f"页面 {BASE}，CPU 池 {state['pool']}，样例 {names}")

    # -- 场景一：互换两个样例的核 -------------------------------------
    before = client.bindings()
    print(f"场景一：互换 {names[0]}({before[names[0]]}) 与 {names[1]}({before[names[1]]})")
    target = dict(before)
    target[names[0]], target[names[1]] = before[names[1]], before[names[0]]
    job = client.submit(target)
    after = client.bindings()
    check(job["result"] == "applied", f"结果 applied（实际 {job['result']}: {job.get('detail')}）", failures)
    check(after == target, f"换绑后绑定 {after} == 目标 {target}", failures)
    check(single_and_distinct(after), "换绑后每进程单核且互不共核", failures)
    used_borrow = any(step["kind"] == "borrow" for step in job["steps"])
    check(used_borrow, "互换借助了空闲核周转（未停进程腾位置）", failures)
    for step in job["steps"]:
        check(
            single_and_distinct(step["readback"]),
            f"步骤 {step['index']} 之后仍各占一核：{step['readback']}",
            failures,
        )
    print("  计划:", job["plan"])

    # -- 场景二：环状换核，第 2 步注入故障，应回退 ---------------------
    before_fail = client.bindings()
    order = names[:3] if len(names) >= 3 else names[:2]
    rotate = {order[i]: before_fail[order[(i + 1) % len(order)]] for i in range(len(order))}
    target = dict(before_fail)
    target.update(rotate)
    print(f"场景二：目标 {target}，第 2 步注入故障")
    job = client.submit(target, fail_at=2)
    restored = client.bindings()
    check(job["result"] == "rolled_back", f"结果 rolled_back（实际 {job['result']}: {job.get('detail')}）", failures)
    check(restored == before_fail, f"回退后绑定 {restored} == 原始 {before_fail}", failures)
    check(single_and_distinct(restored), "回退后每进程单核且互不共核", failures)
    check(len(job["steps"]) >= 1 and job["steps"][0]["status"] == "ok", "故障前至少完成了一步", failures)
    check(job["steps"][-1]["status"] == "failed", "故障步标记为 failed", failures)
    for step in job["rollback"]:
        check(
            single_and_distinct(step["readback"]),
            f"回退步骤 {step['index']} 之后仍各占一核：{step['readback']}",
            failures,
        )
        check(step["status"] == "ok", f"回退步骤 {step['index']} 成功", failures)
    print("  失败原因:", job["detail"])

    print()
    if failures:
        print(f"有 {len(failures)} 项未通过")
        return 1
    print("两个场景全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
