#!/usr/bin/env bash
# 一次跑完单元 + 集成测试（集成测试会真的启动样例进程并调用 sched_setaffinity）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPS="$ROOT/.deps"
if [[ ! -d "$DEPS" ]]; then
  mkdir -p "$DEPS"
  for wheel in "$ROOT"/vendor/*.whl; do
    python3 -m zipfile -e "$wheel" "$DEPS"
  done
fi

export PYTHONPATH="$DEPS:$ROOT${PYTHONPATH:+:$PYTHONPATH}"
cd "$ROOT"
exec python3 -m unittest discover -s tests -t . -v "$@"
