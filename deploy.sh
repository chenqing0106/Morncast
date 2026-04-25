#!/usr/bin/env bash
# 一键部署 Morncast 到远端服务器
#   - rsync 同步代码 + video/ + 本地 warm 的 cache (data/, audio_cache/)
#   - 首次自动建 venv、装依赖；之后只增量同步
#   - 服务器永远不调 Edge TTS（用本地预生成的缓存）
#
# 用法：
#   ./deploy.sh                    # 默认 REMOTE=tencentyun, PORT=8002
#   PORT=8888 ./deploy.sh          # 改端口
#   REMOTE=aliyun-hk ./deploy.sh   # 改目标机器（需在 ~/.ssh/config 里配过）

set -euo pipefail

REMOTE="${REMOTE:-tencentyun}"
REMOTE_DIR="${REMOTE_DIR:-~/morncast}"
PORT="${PORT:-8002}"

echo "→ target  : $REMOTE:$REMOTE_DIR"
echo "→ port    : $PORT"
echo ""

# 0. 确认本地缓存存在（避免推一个空 cache 到服务器）
if [ ! -f audio_cache/morncast.mp3 ] || [ ! -f data/script.json ] || [ ! -f data/timing.json ]; then
  echo "⚠️  本地缓存不全，先在本地跑一次让缓存 warm 起来："
  echo "   curl http://127.0.0.1:8002/api/brief"
  echo ""
  read -p "继续部署（服务器会尝试调 Edge TTS，国内可能失败）？[y/N] " ans
  [[ "$ans" == "y" || "$ans" == "Y" ]] || exit 1
fi

# 1. 同步代码 + video + cache（不加 --delete，避免误删远端 cache）
echo "→ rsync code + video + cache..."
rsync -avz \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '.git/' \
  --exclude '.DS_Store' \
  --exclude 'docs/' \
  --exclude '.env' \
  --exclude 'deploy.sh' \
  --exclude '本地预览扫码指南.md' \
  ./ "$REMOTE:$REMOTE_DIR/"

# 2. .env 单独处理：远端没有才上传（敏感文件，避免覆盖远端已有配置）
if ssh "$REMOTE" "[ -f $REMOTE_DIR/.env ]" 2>/dev/null; then
  echo "→ remote .env exists, kept"
else
  echo "→ remote .env missing, uploading local .env..."
  scp .env "$REMOTE:$REMOTE_DIR/.env"
fi

# 3. 远端装依赖 + 重启
echo "→ remote bootstrap & restart..."
ssh "$REMOTE" "PORT=$PORT REMOTE_DIR=$REMOTE_DIR bash -s" <<'EOF'
set -euo pipefail
cd "$REMOTE_DIR"

# 首次：建 venv 装依赖
if [ ! -d .venv ]; then
  echo "  · creating venv..."
  python3.12 -m venv .venv 2>/dev/null || python3 -m venv .venv
  .venv/bin/pip install --upgrade pip --quiet
  .venv/bin/pip install -r requirements.txt --quiet
else
  # 增量：只在 requirements 变化时重装
  .venv/bin/pip install -r requirements.txt --quiet
fi

mkdir -p audio_cache data

# 杀掉旧进程（仅匹配本项目本端口）
pkill -f "uvicorn server:app.*--port $PORT" 2>/dev/null || true
sleep 1

# 后台启动
nohup .venv/bin/python -m uvicorn server:app \
  --host 0.0.0.0 --port "$PORT" \
  > /tmp/morncast.log 2>&1 &
disown

# 等启动 + 健康检查
for i in 1 2 3 4 5; do
  sleep 1
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    echo "  · server up on :$PORT"
    break
  fi
  if [ "$i" = "5" ]; then
    echo "  ✗ server failed to start, last 20 log lines:"
    tail -20 /tmp/morncast.log
    exit 1
  fi
done
EOF

# 4. 输出公网入口
PUBLIC_IP=$(ssh "$REMOTE" "curl -s ifconfig.me 2>/dev/null || hostname -I | awk '{print \$1}'" 2>/dev/null || echo "")
echo ""
if [ -n "$PUBLIC_IP" ]; then
  echo "✅ deployed: http://$PUBLIC_IP:$PORT"
else
  echo "✅ deployed (公网 IP 未拿到，自己看一下)"
fi
echo ""
echo "查看日志：  ssh $REMOTE 'tail -f /tmp/morncast.log'"
echo "停止服务：  ssh $REMOTE 'pkill -f \"uvicorn server:app.*--port $PORT\"'"
