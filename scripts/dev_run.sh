#!/usr/bin/env bash
# 本机（无 Docker）等价演练：限定 CPU 池，启动样例进程与 Flask 页面。
#
# 池子通过 taskset 限定，与 Compose 的 cpuset 语义一致：
#   ./scripts/dev_run.sh            # 池 0-5，3 个样例，端口 8008
#   POOL=0-3 SAMPLES=2 ./scripts/dev_run.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
POOL="${POOL:-0-5}"
SAMPLES="${SAMPLES:-3}"
PORT="${PORT:-8008}"

DEPS="$ROOT/.deps"
if [[ ! -d "$DEPS" ]]; then
  mkdir -p "$DEPS"
  for wheel in "$ROOT"/vendor/*.whl; do
    python3 -m zipfile -e "$wheel" "$DEPS"
  done
fi

export PYTHONPATH="$DEPS:$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export DRILL_SAMPLES="$SAMPLES"
export DRILL_HOST="127.0.0.1"
export DRILL_PORT="$PORT"

echo "CPU 池: $POOL  样例数: $SAMPLES  页面: http://127.0.0.1:$PORT/"
exec taskset -c "$POOL" python3 -m drill.run --samples "$SAMPLES" --host 127.0.0.1 --port "$PORT" "$@"
