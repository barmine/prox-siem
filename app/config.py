import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


class Config:
    OPENSEARCH_URL = os.environ.get("OPENSEARCH_URL", "http://localhost:9200")
    INDEX_PREFIX = os.environ.get("INDEX_PREFIX", "proxmox-logs")
    SHARED_SECRET = os.environ.get("SHARED_SECRET")
    # Must be one of dashboard.py's PAGE_SIZE_OPTIONS (25/50/100) -- if it
    # isn't, dashboard.index() can't use it and silently falls back to 50
    # instead, making this setting a no-op.
    DASHBOARD_PAGE_SIZE = int(os.environ.get("DASHBOARD_PAGE_SIZE", "50"))

    # Phase 3: detection/scoring layer.
    RULES_PATH = os.environ.get("RULES_PATH", str(REPO_ROOT / "rules.yaml"))
    CLUSTERS_INDEX = os.environ.get("CLUSTERS_INDEX", "proxmox-clusters")
    # How far back the dedup job looks each run. It recomputes clusters
    # from scratch within this window every run rather than keeping a
    # cursor, so a cluster's count/first_seen/last_seen always reflect
    # "within the last DEDUP_LOOKBACK_MINUTES", not an ever-growing total.
    DEDUP_LOOKBACK_MINUTES = int(os.environ.get("DEDUP_LOOKBACK_MINUTES", "30"))
    # Clusters that haven't been refreshed by a run in this long are
    # considered resolved and get deleted from the clusters index.
    DEDUP_RETENTION_MINUTES = int(os.environ.get("DEDUP_RETENTION_MINUTES", "120"))
    DEDUP_MAX_DOCS = int(os.environ.get("DEDUP_MAX_DOCS", "5000"))
    RANKING_HALF_LIFE_MINUTES = int(os.environ.get("RANKING_HALF_LIFE_MINUTES", "60"))
    RANKING_ACTIVE_WINDOW_HOURS = int(os.environ.get("RANKING_ACTIVE_WINDOW_HOURS", "24"))
    RANKING_DEFAULT_LIMIT = int(os.environ.get("RANKING_DEFAULT_LIMIT", "50"))
