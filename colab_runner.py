#!/usr/bin/env python3
"""
colab_runner.py

Automated bootstrapping script executed inside Google Colab by the Watchdog daemon.
1. Installs rclone & quickwit binary.
2. Configures Google Drive token for rclone.
3. Syncs domain_discovery.db & vids2 packages from Google Drive.
4. Starts internal rclone S3 gateway (port 9099) for direct Tantivy split uploads.
5. Launches universal_video_crawler.py in the background with continuous logging.
"""

import os
import sys
import time
import subprocess
import shutil
from pathlib import Path

print("==================================================")
print(" Google Colab Crawler & Direct Drive Indexer Boot ")
print("==================================================")

# 1. Install system prerequisites & rclone if missing
if not shutil.which("rclone"):
    print("[1/6] Installing rclone...")
    subprocess.run("curl -s https://rclone.org/install.sh | bash", shell=True, check=True)

# 2. Install Quickwit if missing
if not shutil.which("quickwit"):
    print("[2/6] Installing Quickwit v0.8.2...")
    subprocess.run("curl -sL https://github.com/quickwit-oss/quickwit/releases/download/v0.8.2/quickwit-v0.8.2-x86_64-unknown-linux-gnu.tar.gz | tar -xz -C /tmp", shell=True, check=True)
    subprocess.run("mv /tmp/quickwit-v0.8.2/quickwit /usr/local/bin/quickwit", shell=True, check=True)

# 3. Configure Google Drive Rclone credentials
print("[3/6] Configuring Google Drive credentials...")
os.makedirs("/root/.config/rclone", exist_ok=True)
token_val = os.environ.get("RCLONE_CONFIG_GDRIVE_TOKEN", "")
if token_val:
    gdrive_conf = f"""[gdrive]
type = drive
scope = drive
token = {token_val}
"""
    with open("/root/.config/rclone/rclone.conf", "w", encoding="utf-8") as f:
        f.write(gdrive_conf)

# 4. Sync Database & packages
print("[4/6] Syncing database and packages from Google Drive...")
os.makedirs("/content/quickwit-data/batches", exist_ok=True)
os.makedirs("/content/quickwit-data/crawler_logs", exist_ok=True)
os.makedirs("/content/db", exist_ok=True)

subprocess.run(["rclone", "copy", "gdrive:quickwit-data/db/domain_discovery.db", "/content/db/"], check=True)
subprocess.run(["rclone", "copy", "gdrive:quickwit-data/packages/vids2_services.tar.gz", "/content/"], check=True)
subprocess.run(["rclone", "copy", "gdrive:quickwit-data/packages/vids2_manifests.tar.gz", "/content/"], check=False)
subprocess.run(["rclone", "copy", "gdrive:quickwit-data/packages/universal_video_crawler.py", "/content/"], check=True)
subprocess.run(["tar", "-xzf", "/content/vids2_services.tar.gz", "-C", "/content/"], check=True)
if os.path.exists("/content/vids2_manifests.tar.gz"):
    os.makedirs("/content/.autopopulator_cache", exist_ok=True)
    subprocess.run(["tar", "-xzf", "/content/vids2_manifests.tar.gz", "-C", "/content/.autopopulator_cache/"], check=True)

# 5. Install Python dependencies
print("[5/6] Installing Python packages...")
subprocess.run(["pip", "install", "-q", "aiohttp", "aiosqlite", "lxml", "curl_cffi", "pydantic", "parsel", "beautifulsoup4"], check=True)

# 6. Start background Rclone S3 bridge
print("[6/6] Launching background rclone S3 gateway & Crawler...")
subprocess.Popen([
    "rclone", "serve", "s3",
    "--addr", "127.0.0.1:9099",
    "--auth-key", "fake,fake",
    "--dir-cache-time", "5s",
    "gdrive:quickwit-data"
])

crawler_env = os.environ.copy()
crawler_env["PYTHONPATH"] = "/content:" + crawler_env.get("PYTHONPATH", "")
crawler_env["CRAWLER_DB_PATH"] = "/content/db/domain_discovery.db"
crawler_env["DB_PROXY_URL"] = "https://vids-search-service.onrender.com"
crawler_env["NDJSON_EXPORT_DIR"] = "/content/quickwit-data/batches"
crawler_env["CRAWLER_LOG_FILE"] = "/content/quickwit-data/crawler_logs/colab_crawler.log"

subprocess.Popen(
    ["python3", "/content/universal_video_crawler.py"],
    env=crawler_env,
    stdout=open("/content/quickwit-data/crawler_logs/stdout.log", "a"),
    stderr=open("/content/quickwit-data/crawler_logs/stderr.log", "a")
)

print("✔ Google Colab environment and Crawler successfully booted!")
