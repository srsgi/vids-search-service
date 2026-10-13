#!/usr/bin/env python3
"""
backfill_gdrive_batches.py

Automated backfill tool that downloads legacy and current crawler batches from Google Drive,
normalizes their timestamps to strict RFC-3339 UTC, and indexes them directly into Quickwit.
"""

import os
import sys
import json
import time
import logging
import subprocess
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("backfill_indexer")

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import direct_gdrive_indexer as dgi

TEMP_DIR = Path("/tmp/quickwit_backfill")
INGESTED_TRACKER = SCRIPT_DIR / "backfilled_batches.json"


def load_ingested_batches() -> set:
    if INGESTED_TRACKER.exists():
        try:
            return set(json.loads(INGESTED_TRACKER.read_text()))
        except Exception:
            return set()
    return set()


def save_ingested_batches(ingested: set):
    try:
        INGESTED_TRACKER.write_text(json.dumps(list(ingested), indent=2))
    except Exception as e:
        logger.error(f"Failed to save ingested tracker: {e}")


def main():
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    ingested_set = load_ingested_batches()
    logger.info(f"Loaded {len(ingested_set)} previously backfilled batches.")

    # 1. List batches on Google Drive
    logger.info("Listing batch files on Google Drive...")
    res = subprocess.run(
        ["rclone", "lsf", "gdrive:quickwit-data/batches/"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if res.returncode != 0:
        logger.error(f"Failed to list batches from Google Drive: {res.stderr}")
        return

    all_batches = sorted([
        f.strip() for f in res.stdout.splitlines()
        if f.strip().endswith(".ndjson") and f.strip() not in ingested_set
    ])
    logger.info(f"Found {len(all_batches)} un-ingested batches to process.")

    if not all_batches:
        logger.info("All batches are already ingested!")
        return

    # Process first batch batch (e.g. 10 batches in this run)
    target_batches = all_batches[:15]
    logger.info(f"Processing next {len(target_batches)} batches...")

    # Start rclone S3 gateway
    rclone_proc = dgi.start_rclone_s3_bridge()
    try:
        qw_cfg = dgi.generate_local_quickwit_config()
        for batch_name in target_batches:
            logger.info(f"Downloading batch {batch_name}...")
            local_batch = TEMP_DIR / batch_name
            dl_res = subprocess.run(
                ["rclone", "copy", f"gdrive:quickwit-data/batches/{batch_name}", str(TEMP_DIR)],
                capture_output=True,
                timeout=60,
            )
            if not local_batch.exists():
                logger.warning(f"Could not download {batch_name}, skipping.")
                continue

            # Ingest with automatic normalization
            dgi.ingest_ndjson(local_batch, qw_cfg, "prn")
            ingested_set.add(batch_name)
            save_ingested_batches(ingested_set)

            # Cleanup local temp file
            if local_batch.exists():
                local_batch.unlink()

        logger.info("✔ Backfill batch run complete!")
    finally:
        rclone_proc.terminate()
        try:
            rclone_proc.wait(timeout=3)
        except Exception:
            rclone_proc.kill()


if __name__ == "__main__":
    main()
