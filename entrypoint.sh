#!/usr/bin/env bash
set -e

echo "=========================================="
echo " Starting Vids Search Service (Quickwit)   "
echo "=========================================="

# 1. Start rclone S3 gateway to Google Drive in the background
echo "[1/4] Starting internal rclone S3 gateway to Google Drive..."
rclone serve s3 \
  --addr 127.0.0.1:9000 \
  --auth-key fake,fake \
  --dir-cache-time 1m \
  gdrive:quickwit-data &
RCLONE_PID=$!

echo "[2/4] Waiting for rclone S3 gateway on 127.0.0.1:9000..."
for i in $(seq 1 30); do
  if curl -s http://127.0.0.1:9000 > /dev/null 2>&1 || [ $? -eq 52 ]; then
    echo "rclone S3 gateway is UP and connected to Google Drive."
    break
  fi
  sleep 1
done

# 2. Configure Quickwit listen port
PORT="${PORT:-10000}"
echo "[3/4] Configuring Quickwit to listen on port: ${PORT}"
sed -i "s/listen_port: .*/listen_port: ${PORT}/" /app/quickwit.yaml

# 3. Background index initializer
(
  echo "Monitoring Quickwit readiness for auto index creation..."
  for i in $(seq 1 45); do
    if curl -s "http://127.0.0.1:${PORT}/health" > /dev/null 2>&1 || curl -s "http://127.0.0.1:${PORT}/api/v1/version" > /dev/null 2>&1; then
      echo "Quickwit REST API is live! Checking if index 'prn' exists..."
      if ! curl -s -f "http://127.0.0.1:${PORT}/api/v1/indexes/prn" > /dev/null 2>&1; then
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

# 4. Start Quickwit server in foreground
echo "[4/4] Starting Quickwit service..."
exec quickwit run --service searcher,indexer --config /app/quickwit.yaml
