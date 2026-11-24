#!/usr/bin/env python3
"""Entry point used by Docker Compose and by local runs.

Starts the sample processes, verifies they are single-threaded and bound to
distinct single cores, then serves the drill page.

Usage::

    python -m drill.run                # 3 samples (DRILL_SAMPLES to change)
    python -m drill.run --check        # start, self-check, print state, exit
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys

from . import kernel
from .cpulist import format_cpu_list
from .session import MAX_SAMPLES, MIN_SAMPLES, Session, describe_exception


def _banner(session: Session) -> None:
    samples = session.samples_state()
    print(f"容器可用 CPU 池: {format_cpu_list(session.pool)}")
    print(f"样例进程数: {len(samples)}")
    for row in samples:
        print(
            f"  {row['name']:<6} pid={row['pid']:<7} threads={row['threads']} "
            f"Cpus_allowed_list={row['cpus'] or '-'} state={row['state']}"
        )


def check(session: Session) -> int:
    rows = session.samples_state()
    problems = []
    if not MIN_SAMPLES <= len(rows) <= MAX_SAMPLES:
        problems.append(f"样例进程数 {len(rows)} 不在 {MIN_SAMPLES}～{MAX_SAMPLES}")
    used = [row["cpus"] for row in rows]
    if len(set(used)) != len(used):
        problems.append(f"样例进程存在共核：{used}")
    for row in rows:
        if row["threads"] != 1:
            problems.append(f"{row['name']} 有 {row['threads']} 个线程，不是单线程")
        if "," in row["cpus"] or "-" in row["cpus"]:
            problems.append(f"{row['name']} 绑定 {row['cpus']} 不是单核")
        if not row["alive"]:
            problems.append(f"{row['name']} 不存活")
    payload = {
        "pool": format_cpu_list(session.pool),
        "samples": rows,
        "cores": session.cores(),
        "problems": problems,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not problems else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="收包进程换核演练")
    parser.add_argument("--samples", type=int, default=int(os.environ.get("DRILL_SAMPLES", "3")))
    parser.add_argument("--host", default=os.environ.get("DRILL_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("DRILL_PORT", "8008")))
    parser.add_argument("--check", action="store_true", help="启动自检后退出")
    args = parser.parse_args(argv)

    if not MIN_SAMPLES <= args.samples <= MAX_SAMPLES:
        print(f"样例数必须在 {MIN_SAMPLES}～{MAX_SAMPLES} 之间", file=sys.stderr)
        return 2

    session = Session(sample_count=args.samples, pool=kernel.cpu_pool())
    try:
        session.start()
    except Exception as exc:  # noqa: BLE001 - startup must fail loudly
        print(f"启动样例进程失败：{describe_exception(exc)}", file=sys.stderr)
        return 3

    _banner(session)
    if args.check:
        code = check(session)
        session.shutdown()
        return code

    from .web import create_app

    app = create_app(session)

    def _stop(signum, frame):  # noqa: ARG001
        print(f"\n收到信号 {signum}，停止样例进程…")
        session.shutdown()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    print(f"演练页: http://{args.host}:{args.port}/")
    try:
        app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)
    finally:
        session.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
