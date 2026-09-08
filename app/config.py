import os


class Config:
    OPENSEARCH_URL = os.environ.get("OPENSEARCH_URL", "http://localhost:9200")
    INDEX_PREFIX = os.environ.get("INDEX_PREFIX", "proxmox-logs")
    SHARED_SECRET = os.environ.get("SHARED_SECRET")
    DASHBOARD_PAGE_SIZE = int(os.environ.get("DASHBOARD_PAGE_SIZE", "200"))
