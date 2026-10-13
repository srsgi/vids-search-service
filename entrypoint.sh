#!/usr/bin/env bash
set -e

echo "=========================================="
echo " Starting Vids Search Service (Quickwit)   "
echo "=========================================="

# 1. Setup rclone configuration file if token is provided
if [ -n "$RCLONE_CONFIG_GDRIVE_TOKEN" ]; then
  echo "Writing rclone.conf from environment..."
  mkdir -p /root/.config/rclone
  cat <<EOF > /root/.config/rclone/rclone.conf
[gdrive]
type = drive
scope = drive
token = ${RCLONE_CONFIG_GDRIVE_TOKEN}
EOF
fi

# 2. Start internal rclone S3 gateway to Google Drive in the background
echo "[1/4] Starting internal rclone S3 gateway to Google Drive..."
rclone serve s3 \
  --addr 127.0.0.1:9000 \
  --auth-key fake,fake \
  --dir-cache-time 5s \
  gdrive:quickwit-data &

echo "[2/4] Waiting for rclone S3 gateway on 127.0.0.1:9000..."
for i in $(seq 1 30); do
  if curl -s http://127.0.0.1:9000 > /dev/null 2>&1 || [ $? -eq 52 ]; then
    echo "rclone S3 gateway is UP and connected to Google Drive."
    break
  fi
  sleep 1
done

# 3. Configure Quickwit internal listen port
QW_PORT="10001"
echo "[3/5] Configuring Quickwit internal listen port: ${QW_PORT}"
sed -i "s/listen_port: .*/listen_port: ${QW_PORT}/" /app/quickwit.yaml

# 4. Sync domain_discovery.db from Google Drive
echo "[4/5] Syncing domain_discovery.db from Google Drive..."
mkdir -p /app/db
if [ -n "$RCLONE_CONFIG_GDRIVE_TOKEN" ]; then
  rclone copy gdrive:quickwit-data/db/domain_discovery.db /app/db/ || true
fi

# Periodically sync db back to Google Drive every 10 minutes in background
(
  while true; do
    sleep 600
    if [ -f "/app/db/domain_discovery.db" ] && [ -n "$RCLONE_CONFIG_GDRIVE_TOKEN" ]; then
      rclone copy /app/db/domain_discovery.db gdrive:quickwit-data/db/ || true
    fi
  done
) &

# 5. Background index initializer
(
  echo "Monitoring Quickwit readiness for auto index creation..."
  for i in $(seq 1 45); do
    if curl -s "http://127.0.0.1:${QW_PORT}/health" > /dev/null 2>&1 || curl -s "http://127.0.0.1:${QW_PORT}/api/v1/version" > /dev/null 2>&1; then
      echo "Quickwit REST API is live on internal port ${QW_PORT}! Checking if index 'prn' exists..."
      if ! curl -s -f "http://127.0.0.1:${QW_PORT}/api/v1/indexes/prn" > /dev/null 2>&1; then
        echo "Creating 'prn' index in Google Drive storage..."
        quickwit index create --index-config /app/index-config.yaml --config /app/quickwit.yaml || true
      else
        echo "Index 'prn' already initialized."
      fi
      break
    fi
    sleep 1
  done
) &

# 6. Start Quickwit service in background on internal port
echo "Starting Quickwit service on internal port ${QW_PORT}..."
mkdir -p /app/qwdata
quickwit run --service searcher --service metastore --config /app/quickwit.yaml &
QW_PID=$!

trap "kill -TERM $QW_PID 2>/dev/null || true; [ -f /app/db/domain_discovery.db ] && rclone copy /app/db/domain_discovery.db gdrive:quickwit-data/db/ || true; exit" SIGTERM SIGINT

# 7. Start unified FastAPI Gateway on Render's public $PORT in foreground
PORT="${PORT:-10000}"
echo "[5/5] Starting Unified FastAPI Gateway on public port ${PORT}..."
export QUICKWIT_INTERNAL_URL="http://127.0.0.1:${QW_PORT}"
export PROXY_DB_PATH="/app/db/domain_discovery.db"
exec python3 -m uvicorn db_proxy:app --host 0.0.0.0 --port "${PORT}"
