import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


class Config:
    OPENSEARCH_URL = os.environ.get("OPENSEARCH_URL", "http://localhost:9200")
    INDEX_PREFIX = os.environ.get("INDEX_PREFIX", "proxmox-logs")
    SHARED_SECRET = os.environ.get("SHARED_SECRET")
    # Must be one of dashboard.py's PAGE_SIZE_OPTIONS (25/50/100) -- if it
    # isn't, dashboard.explore() can't use it and silently falls back to 50
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
    # considered resolved and get deleted from the clusters index. Phase 4
    # bumped the default from 2h to 7d so Overview's 7d/custom range
    # picker has resolved clusters to actually show -- this only affects
    # how long a quiet cluster stays queryable, not the 30m
    # DEDUP_LOOKBACK_MINUTES window used to compute "what's live right now".
    DEDUP_RETENTION_MINUTES = int(os.environ.get("DEDUP_RETENTION_MINUTES", str(7 * 24 * 60)))
    DEDUP_MAX_DOCS = int(os.environ.get("DEDUP_MAX_DOCS", "5000"))
    RANKING_HALF_LIFE_MINUTES = int(os.environ.get("RANKING_HALF_LIFE_MINUTES", "60"))
    RANKING_ACTIVE_WINDOW_HOURS = int(os.environ.get("RANKING_ACTIVE_WINDOW_HOURS", "24"))
    RANKING_DEFAULT_LIMIT = int(os.environ.get("RANKING_DEFAULT_LIMIT", "50"))

    # Phase 4: pipeline health -- how long a source can go quiet before
    # Pipeline Health flags it as stale rather than just idle.
    PIPELINE_HEARTBEAT_STALE_MINUTES = int(os.environ.get("PIPELINE_HEARTBEAT_STALE_MINUTES", "5"))
    PIPELINE_WEBHOOK_STALE_HOURS = int(os.environ.get("PIPELINE_WEBHOOK_STALE_HOURS", "48"))

    # Phase 4: outbound alerting. Disabled by default -- no webhook target
    # is configured out of the box, so this can't break on a fresh deploy.
    ALERT_ENABLED = os.environ.get("ALERT_ENABLED", "false").lower() in ("1", "true", "yes")
    # "ntfy" (default), "discord", or "gotify" -- see app/alerting.py.
    ALERT_TARGET_TYPE = os.environ.get("ALERT_TARGET_TYPE", "ntfy")
    # Full target URL (ntfy topic URL / Discord webhook URL / Gotify
    # ".../message?token=..."). Swapping targets is a config change, not
    # a code change.
    ALERT_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "")

    # Phase 5: volume-baseline anomaly detection -- a second detection
    # path alongside rules.yaml's pattern matching, for the thing pattern
    # rules structurally can't catch: a normally-quiet log type suddenly
    # spiking, with no single line matching a known-bad regex.
    ANOMALY_BASELINE_DAYS = int(os.environ.get("ANOMALY_BASELINE_DAYS", "14"))
    # Floor below which a volume difference isn't worth flagging, no
    # matter the z-score -- a source going from 1 event/hour to 3 is
    # statistically loud but operationally meaningless.
    ANOMALY_MIN_COUNT = int(os.environ.get("ANOMALY_MIN_COUNT", "5"))
    ANOMALY_ZSCORE_THRESHOLD = float(os.environ.get("ANOMALY_ZSCORE_THRESHOLD", "3.0"))
    ANOMALY_ZSCORE_CRITICAL = float(os.environ.get("ANOMALY_ZSCORE_CRITICAL", "6.0"))
    # Fallback for a zero-variance baseline (a normally-silent source --
    # z-score is undefined when stdev is 0), e.g. current >= mean * this.
    ANOMALY_MULTIPLIER_FALLBACK = float(os.environ.get("ANOMALY_MULTIPLIER_FALLBACK", "5.0"))
    # Cap on distinct (hostname, unit) sources evaluated per run.
    ANOMALY_MAX_SOURCES = int(os.environ.get("ANOMALY_MAX_SOURCES", "200"))

    # Phase 5: noise suppression. Muted (rule_id, hostname) pairs are
    # dropped from clustering/ranking (app/dedup.py, app/anomaly.py) so
    # confirmed-benign recurring matches stop cluttering Overview, while
    # the raw tagged events stay fully queryable in Explore.
    MUTES_INDEX = os.environ.get("MUTES_INDEX", "proxmox-mutes")
