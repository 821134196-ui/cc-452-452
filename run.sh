#!/usr/bin/env bash
# 物流园挂车甩挂与车钥匙交接平台 —— 一条命令本地启动
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
DB="${DB:-data/yard.db}"
PHOTOS="${PHOTOS:-data/photos}"
RESEED=0
[[ "${1:-}" == "--reseed" ]] && { RESEED=1; shift; }

# 首次启动自动加载高峰样例；--reseed 强制重建
if [[ ! -f "$DB" || "$RESEED" == "1" ]]; then
  echo ">> 加载高峰样例数据 ..."
  python3 -m platform.seed --db "$DB" --photos "$PHOTOS" --reset
fi

echo ">> 启动平台：http://localhost:${PORT}"
exec python3 -m platform.server --host 0.0.0.0 --port "$PORT" \
  --db "$DB" --photos "$PHOTOS" "$@"
