#!/usr/bin/env bash
# 一键演示：从空目录启动三服务 -> 跑端到端场景 -> 收尾保留数据
set -euo pipefail

cd "$(dirname "$0")"

echo ">> 清理旧数据并释放端口"
pkill -f "run_services.py" 2>/dev/null || true
sleep 1
rm -rf data

echo ">> 启动三服务（intake:8101 freezer:8102 reporting:8103）"
python3 -I run_services.py --service all > /tmp/trialpipe-services.log 2>&1 &
SVC_PID=$!
trap 'kill "$SVC_PID" 2>/dev/null || true' EXIT

# 等待健康检查通过
for i in $(seq 1 30); do
  if curl -sf http://127.0.0.1:8101/health >/dev/null \
     && curl -sf http://127.0.0.1:8102/health >/dev/null \
     && curl -sf http://127.0.0.1:8103/health >/dev/null; then
    break
  fi
  sleep 0.5
done

echo ">> 运行端到端演示"
python3 -I demo_scenario.py

echo
echo ">> 演示结束。数据保留在 ./data，服务日志 /tmp/trialpipe-services.log"
