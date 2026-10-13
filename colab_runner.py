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
if os.path.exists("/tmp/rclone.conf"):
    shutil.copy("/tmp/rclone.conf", "/root/.config/rclone/rclone.conf")
token_val = os.environ.get("RCLONE_CONFIG_GDRIVE_TOKEN", "")
if token_val and not os.path.exists("/root/.config/rclone/rclone.conf"):
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

# 6. Generate and launch persistent Crawler Watchdog & Heartbeat Daemon
print("[6/6] Launching persistent Crawler Watchdog & Heartbeat Daemon...")

daemon_script = '''#!/usr/bin/env python3
import os
import sys
import time
import subprocess
import json
import urllib.request

PROXY_URL = "https://vids-search-service.onrender.com"
LOG_FILE = "/content/quickwit-data/crawler_logs/colab_crawler.log"
BATCH_DIR = "/content/quickwit-data/batches"
os.makedirs("/content/quickwit-data/crawler_logs", exist_ok=True)
os.makedirs(BATCH_DIR, exist_ok=True)

# Start internal Rclone S3 bridge (port 9099)
if subprocess.run(["pgrep", "-f", "rclone serve s3"], capture_output=True).returncode != 0:
    subprocess.Popen([
        "rclone", "serve", "s3",
        "--addr", "127.0.0.1:9099",
        "--auth-key", "fake,fake",
        "--dir-cache-time", "5s",
        "gdrive:quickwit-data"
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

env = os.environ.copy()
env["PYTHONPATH"] = "/content:" + env.get("PYTHONPATH", "")
env["CRAWLER_DB_PATH"] = "/content/db/domain_discovery.db"
env["DB_PROXY_URL"] = PROXY_URL
env["NDJSON_EXPORT_DIR"] = BATCH_DIR
env["CRAWLER_LOG_FILE"] = LOG_FILE

while True:
    # Check if universal_video_crawler is running
    is_running = subprocess.run(["pgrep", "-f", "universal_video_crawler.py"], capture_output=True).returncode == 0
    if not is_running:
        out_f = open(LOG_FILE, "a")
        subprocess.Popen(
            ["python3", "-u", "/content/universal_video_crawler.py"],
            env=env,
            stdout=out_f,
            stderr=out_f,
            start_new_session=True,
        )

    # Sync logs and batches to Google Drive
    try:
        subprocess.run(["rclone", "copy", "/content/quickwit-data/crawler_logs/", "gdrive:quickwit-data/crawler_logs/"], timeout=30, capture_output=True)
        subprocess.run(["rclone", "copy", BATCH_DIR, "gdrive:quickwit-data/batches/"], timeout=30, capture_output=True)
    except Exception:
        pass

    # Send heartbeat to Render
    try:
        batch_count = len(os.listdir(BATCH_DIR)) if os.path.exists(BATCH_DIR) else 0
        log_snippet = ""
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "r", errors="ignore") as f:
                f.seek(max(0, os.path.getsize(LOG_FILE) - 1000))
                log_snippet = f.read()

        hb_payload = {
            "crawler_id": "vids-crawler",
            "status": "running",
            "metrics": {
                "batches_written": batch_count,
                "timestamp": time.time(),
            },
            "log_snippet": log_snippet[-400:] if log_snippet else None,
        }
        req = urllib.request.Request(
            f"{PROXY_URL}/api/crawler/heartbeat",
            data=json.dumps(hb_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass

    time.sleep(30)
'''

with open("/content/run_crawler_daemon.py", "w", encoding="utf-8") as f:
    f.write(daemon_script)

subprocess.Popen(["python3", "/content/run_crawler_daemon.py"], start_new_session=True)

print("✔ Google Colab persistent daemon and Crawler successfully booted!")
