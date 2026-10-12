#!/usr/bin/env python3
"""
Direct Google Drive Quickwit Indexer (Decoupled from Render Search Service)

This script indexes video documents directly into your Google Drive Quickwit index
splits without passing through Render. Render serves searches over these splits.

Usage:
  # Ingest an NDJSON file:
  python3 direct_gdrive_indexer.py --input-path /path/to/videos.ndjson

  # Export and ingest directly from local Elasticsearch 'prn' index:
  python3 direct_gdrive_indexer.py --from-elasticsearch --limit 1000
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import List, Dict, Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("gdrive_indexer")

SCRIPT_DIR = Path(__file__).resolve().parent
INDEX_CONFIG_PATH = SCRIPT_DIR / "index-config.yaml"
QUICKWIT_CONFIG_PATH = SCRIPT_DIR / "quickwit.yaml"
LOCAL_DATA_DIR = Path("/tmp/qwdata_ingester")
S3_GATEWAY_PORT = 9099


def ensure_tools():
    """Verify rclone and quickwit are installed."""
    if not subprocess.run(["which", "rclone"], capture_output=True).returncode == 0:
        logger.error("rclone is not installed. Install via: curl https://rclone.org/install.sh | sudo bash")
        sys.exit(1)
    if not subprocess.run(["which", "quickwit"], capture_output=True).returncode == 0:
        logger.error("quickwit is not installed. Download from https://github.com/quickwit-oss/quickwit/releases")
        sys.exit(1)


def start_rclone_s3_bridge() -> subprocess.Popen:
    """Start local rclone S3 gateway to Google Drive."""
    logger.info(f"Starting rclone S3 bridge on 127.0.0.1:{S3_GATEWAY_PORT} -> gdrive:quickwit-data...")
    cmd = [
        "rclone", "serve", "s3",
        "--addr", f"127.0.0.1:{S3_GATEWAY_PORT}",
        "--auth-key", "fake,fake",
        "--dir-cache-time", "5s",
        "gdrive:quickwit-data"
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    # Wait for gateway to become available
    for _ in range(30):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{S3_GATEWAY_PORT}", timeout=1):
                break
        except Exception:
            time.sleep(0.5)
    else:
        logger.warning("rclone S3 bridge did not respond within 15s, continuing anyway...")
    return proc


def generate_local_quickwit_config() -> Path:
    """Generate temporary Quickwit YAML pointing to the local rclone S3 bridge."""
    LOCAL_DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp_yaml = LOCAL_DATA_DIR / "quickwit_local.yaml"
    cfg = f"""version: 0.8
node_id: direct-indexer-{int(time.time())}
cluster_id: quickwit-cluster
data_dir: {LOCAL_DATA_DIR}

storage:
  s3:
    flavor: minio
    endpoint: http://127.0.0.1:{S3_GATEWAY_PORT}

metastore_uri: s3://vids-quickwit/metastore
default_index_root_uri: s3://vids-quickwit/indexes
"""
    temp_yaml.write_text(cfg)
    return temp_yaml


import re


def parse_numeric_resolution(val: Any) -> Any:
    """Normalizes resolution strings (e.g. '1080p', '4k', '720', '1920x1080') into vertical height integer (e.g. 1080, 2160, 720)."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val) if val > 0 else None
    s = str(val).strip().lower()
    if not s or s in ("unknown", "none", "null"):
        return None
    if "4k" in s or "2160" in s:
        return 2160
    if "2k" in s or "1440" in s:
        return 1440
    if "1080" in s or "fhd" in s:
        return 1080
    if "720" in s or "hd" in s:
        return 720
    if "480" in s:
        return 480
    if "360" in s:
        return 360
    if "240" in s:
        return 240
    m = re.search(r"(\d{3,4})[pP]?", s)
    if m:
        return int(m.group(1))
    return None


def export_from_elasticsearch(es_url: str = "http://localhost:9200", index: str = "prn", limit: int = 1000) -> Path:
    """Export documents from local Elasticsearch to temporary NDJSON file."""
    output_path = LOCAL_DATA_DIR / "es_export.ndjson"
    logger.info(f"Exporting up to {limit} documents from {es_url}/{index}...")
    
    query = {"query": {"match_all": {}}, "size": limit}
    req = urllib.request.Request(f"{es_url}/{index}/_search", 
                                 headers={"Content-Type": "application/json"},
                                 data=json.dumps(query).encode())
    with urllib.request.urlopen(req) as resp:
        res = json.loads(resp.read().decode())
    
    hits = res.get("hits", {}).get("hits", [])
    logger.info(f"Retrieved {len(hits)} records from Elasticsearch.")
    
    with open(output_path, "w", encoding="utf-8") as f:
        for hit in hits:
            doc = hit.get("_source", {})
            if "resolution" in doc:
                doc["resolution"] = parse_numeric_resolution(doc.get("resolution"))
            f.write(json.dumps(doc) + "\n")
            
    logger.info(f"Wrote NDJSON export to {output_path}")
    return output_path


def ingest_ndjson(input_path: Path, qw_config_path: Path, index_id: str = "prn"):
    """Run quickwit tool local-ingest to build and push splits to Google Drive."""
    logger.info(f"Indexing {input_path} into '{index_id}' and pushing directly to Google Drive...")
    
    env = os.environ.copy()
    env["AWS_ACCESS_KEY_ID"] = "fake"
    env["AWS_SECRET_ACCESS_KEY"] = "fake"
    env["AWS_REGION"] = "us-east-1"
    
    cmd = [
        "quickwit", "tool", "local-ingest",
        "--index", index_id,
        "--input-path", str(input_path),
        "--config", str(qw_config_path),
        "--yes"
    ]
    
    res = subprocess.run(cmd, env=env)
    if res.returncode == 0:
        logger.info("✔ Documents successfully indexed directly to Google Drive!")
    else:
        logger.error(f"Ingest failed with return code {res.returncode}")


def main():
    parser = argparse.ArgumentParser(description="Direct Google Drive Quickwit Indexer")
    parser.add_argument("--input-path", type=str, help="Path to NDJSON file with video metadata")
    parser.add_argument("--from-elasticsearch", action="store_true", help="Export from local Elasticsearch and ingest")
    parser.add_argument("--es-url", type=str, default="http://localhost:9200", help="Elasticsearch URL")
    parser.add_argument("--index", type=str, default="prn", help="Quickwit index name")
    parser.add_argument("--limit", type=int, default=1000, help="Max docs to export from Elasticsearch")
    args = parser.parse_args()

    ensure_tools()
    
    # 1. Start internal S3 gateway
    rclone_proc = start_rclone_s3_bridge()
    try:
        qw_cfg = generate_local_quickwit_config()
        
        # 2. Determine input
        if args.from_elasticsearch:
            ndjson_file = export_from_elasticsearch(args.es_url, args.index, args.limit)
        elif args.input_path:
            ndjson_file = Path(args.input_path)
            if not ndjson_file.exists():
                logger.error(f"File not found: {ndjson_file}")
                return
        else:
            logger.error("Please specify --input-path or --from-elasticsearch")
            parser.print_help()
            return
            
        # 3. Perform direct ingestion
        ingest_ndjson(ndjson_file, qw_cfg, args.index)
        
    finally:
        logger.info("Stopping local rclone S3 bridge...")
        rclone_proc.terminate()
        try:
            rclone_proc.wait(timeout=3)
        except Exception:
            rclone_proc.kill()


if __name__ == "__main__":
    main()
