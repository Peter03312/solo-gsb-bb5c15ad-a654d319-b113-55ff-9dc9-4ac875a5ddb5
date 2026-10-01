#!/bin/sh
# 容器入口：初始化依赖（已在镜像内安装）、建表、按需写入演示数据，然后启动服务。
set -e

echo "[entrypoint] 初始化数据库 ..."
python -c "from app.db import init_db; init_db()"

if [ "${SEED_ON_START:-true}" = "true" ]; then
  echo "[entrypoint] 写入演示数据（库非空时自动跳过）..."
  python -m app.seed || echo "[entrypoint] 演示数据初始化跳过（可能已存在）"
fi

echo "[entrypoint] 启动 uvicorn: ${HOST:-0.0.0.0}:${PORT:-8080}"
exec uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8080}"
