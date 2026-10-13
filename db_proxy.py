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
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, HTTPException, Header, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("DBProxy")

DB_PATH = os.environ.get("PROXY_DB_PATH", "/app/db/domain_discovery.db")
API_KEY = os.environ.get("DB_PROXY_API_KEY", "")
QW_INTERNAL_URL = os.environ.get("QUICKWIT_INTERNAL_URL", "http://127.0.0.1:10001")

http_client: Optional[httpx.AsyncClient] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(base_url=QW_INTERNAL_URL, timeout=60.0)
    yield
    if http_client:
        await http_client.aclose()

app = FastAPI(title="Vids Domain DB Proxy & Search Gateway", version="1.1.0", lifespan=lifespan)


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
async def health_check() -> Dict[str, Any]:
    db_exists = os.path.exists(DB_PATH)
    size = os.path.getsize(DB_PATH) if db_exists else 0
    qw_status = "uninitialized"
    if http_client:
        try:
            r = await http_client.get("/health", timeout=2.0)
            qw_status = "live" if r.status_code == 200 else f"http_{r.status_code}"
        except Exception as e:
            qw_status = f"down ({type(e).__name__})"
    return {
        "status": "healthy",
        "quickwit": qw_status,
        "db_exists": db_exists,
        "db_size_bytes": size,
        "db_path": DB_PATH,
    }


@app.api_route("/api/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD"])
async def proxy_quickwit(path: str, request: Request) -> Response:
    """Transparently proxies Quickwit search, version, and index operations to internal Quickwit."""
    if not http_client:
        raise HTTPException(status_code=503, detail="Quickwit gateway client not ready")

    excluded_headers = {"host", "content-length", "connection", "transfer-encoding"}
    req_headers = {k: v for k, v in request.headers.items() if k.lower() not in excluded_headers}

    target_url = f"/api/v1/{path}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    body = await request.body()
    try:
        rp_req = http_client.build_request(
            method=request.method,
            url=target_url,
            headers=req_headers,
            content=body,
        )
        rp_resp = await http_client.send(rp_req, stream=True)

        resp_headers = {k: v for k, v in rp_resp.headers.items() if k.lower() not in excluded_headers}
        return StreamingResponse(
            rp_resp.aiter_raw(),
            status_code=rp_resp.status_code,
            headers=resp_headers,
            background=BackgroundTask(rp_resp.aclose),
        )
    except Exception as e:
        logger.error(f"Quickwit proxy error for {target_url}: {e}")
        raise HTTPException(status_code=502, detail=f"Bad Gateway (Quickwit): {str(e)}")


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
