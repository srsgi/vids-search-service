# Vids Search Service

Cloud search engine for Vids-2 video catalog powered by Quickwit over Google Drive storage (S3 gateway bridge via Rclone).

### Endpoints
* **Health**: `GET /health`
* **Version**: `GET /api/v1/version`
* **Search**: `POST /api/v1/indexes/prn/search`
* **Ingest**: `POST /api/v1/indexes/prn/ingest`
* **Elasticsearch Search**: `POST /_elastic/prn/_search`
