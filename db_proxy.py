"""
db_proxy.py - High-Performance SQLite REST API Proxy for domain_discovery.db

Runs alongside Quickwit on Render.
Directly accesses Google Drive domain_discovery.db synced locally via rclone or local mount.
Provides fast query execution, batch status updates, and next-crawler-ready feeds.
"""

import os
import sqlite3
import json
import logging
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field
from fastapi import FastAPI, HTTPException, Header, Depends, Query

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("DBProxy")

DB_PATH = os.environ.get("PROXY_DB_PATH", "/app/db/domain_discovery.db")
API_KEY = os.environ.get("DB_PROXY_API_KEY", "")

app = FastAPI(title="Vids Domain DB Proxy", version="1.0.0")


def verify_api_key(x_api_key: Optional[str] = Header(None)) -> bool:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid API key")
    return True


def get_connection() -> sqlite3.Connection:
    if not os.path.exists(DB_PATH):
        raise HTTPException(status_code=503, detail=f"Database file not ready at {DB_PATH}")
    con = sqlite3.connect(DB_PATH, timeout=30.0)
    con.row_factory = sqlite3.Row
    # Enable WAL mode for high concurrent read performance
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA busy_timeout=30000;")
    return con


class QueryRequest(BaseModel):
    query: str
    params: List[Any] = Field(default_factory=list)


class QueryResponse(BaseModel):
    rows: List[Dict[str, Any]]
    rowcount: int


class UpdateStatusRequest(BaseModel):
    domain: str
    status: str
    manifest_status: Optional[str] = None
    stream_status: Optional[str] = None
    regex_pattern: Optional[str] = None
    video_urls: Optional[str] = None
    downloader_metadata: Optional[str] = None
    error_message: Optional[str] = None


@app.get("/health")
def health_check() -> Dict[str, Any]:
    db_exists = os.path.exists(DB_PATH)
    size = os.path.getsize(DB_PATH) if db_exists else 0
    return {
        "status": "healthy",
        "db_exists": db_exists,
        "db_size_bytes": size,
        "db_path": DB_PATH,
    }


@app.get("/api/domains/next-crawler-ready", dependencies=[Depends(verify_api_key)])
def get_next_crawler_ready(limit: int = Query(default=100, le=1000)) -> List[Dict[str, Any]]:
    """Returns the next batch of CRAWLER_READY domains."""
    with get_connection() as con:
        cur = con.cursor()
        cur.execute(
            """
            SELECT id, domain, regex_pattern, video_urls, downloader_metadata 
            FROM domains 
            WHERE status = 'CRAWLER_READY' 
            ORDER BY id ASC 
            LIMIT ?
            """,
            (limit,)
        )
        return [dict(r) for r in cur.fetchall()]


@app.post("/api/domains/update-status", dependencies=[Depends(verify_api_key)])
def update_domain_status(req: UpdateStatusRequest) -> Dict[str, Any]:
    """Updates a domain's status, regex, and metadata."""
    clean_domain = req.domain.replace("https://", "").replace("http://", "").strip("/").lower()
    with get_connection() as con:
        cur = con.cursor()
        cur.execute(
            """
            UPDATE domains 
            SET status = ?,
                manifest_status = COALESCE(?, manifest_status),
                stream_status = COALESCE(?, stream_status),
                regex_pattern = COALESCE(?, regex_pattern),
                video_urls = COALESCE(?, video_urls),
                downloader_metadata = COALESCE(?, downloader_metadata),
                error_message = ?,
                updated_at = datetime('now', 'localtime')
            WHERE LOWER(domain) = ?
            """,
            (
                req.status,
                req.manifest_status,
                req.stream_status,
                req.regex_pattern,
                req.video_urls,
                req.downloader_metadata,
                req.error_message,
                clean_domain,
            )
        )
        con.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"Domain {clean_domain} not found")
        return {"success": True, "domain": clean_domain, "new_status": req.status}


@app.post("/api/db/query", dependencies=[Depends(verify_api_key)])
def execute_query(req: QueryRequest) -> QueryResponse:
    """Safely executes a parameterized SQL query."""
    q_lower = req.query.strip().lower()
    is_select = q_lower.startswith("select") or q_lower.startswith("pragma")
    
    with get_connection() as con:
        cur = con.cursor()
        cur.execute(req.query, tuple(req.params))
        if is_select:
            rows = [dict(r) for r in cur.fetchall()]
            return QueryResponse(rows=rows, rowcount=len(rows))
        else:
            con.commit()
            return QueryResponse(rows=[], rowcount=cur.rowcount)
