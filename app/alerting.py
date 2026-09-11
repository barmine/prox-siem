"""Phase 4/5: outbound alerting. Fires when a cluster crosses into critical
severity for the first time -- the "first time" check is
apply_alert_throttle() below, shared by app/dedup.py (pattern-rule
clusters) and app/anomaly.py (Phase 5 volume-anomaly clusters).

ALERT_TARGET_TYPE picks the payload shape; ALERT_WEBHOOK_URL is always the
*full* target URL (an ntfy topic URL, a Discord webhook URL, a Gotify
".../message?token=..." URL). Swapping targets is a config change, not a
code change -- adding a new target type later is one more small function
plus a dispatch-table entry.
"""
import json
import urllib.request

from opensearchpy.exceptions import NotFoundError


def _build_message(cluster):
    hostname = cluster.get("hostname", "unknown")
    category = cluster.get("category", "Uncategorized")
    rule_id = cluster.get("rule_id", "unknown-rule")
    sample = cluster.get("sample_message") or "(no message)"
    count = cluster.get("count", 1)

    title = f"prox-siem: new critical -- {category} on {hostname}"
    body = f"[{rule_id}] {sample}\n×{count} on {hostname}"
    return title, body


def _send_ntfy(url, title, body):
    req = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        method="POST",
        headers={"Title": title, "Priority": "urgent", "Tags": "rotating_light"},
    )
    urllib.request.urlopen(req, timeout=10)


def _send_discord(url, title, body):
    payload = json.dumps({"content": f"**{title}**\n{body}"}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST", headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10)


def _send_gotify(url, title, body):
    payload = json.dumps({"title": title, "message": body, "priority": 8}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST", headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10)


_SENDERS = {"ntfy": _send_ntfy, "discord": _send_discord, "gotify": _send_gotify}


def send_alert(config, cluster):
    """Returns True if a send was attempted, False if alerting is
    disabled/unconfigured. Network/HTTP errors are NOT swallowed here --
    app/dedup.py's caller decides whether one broken target should stop
    the rest of a dedup run."""
    if not config.get("ALERT_ENABLED") or not config.get("ALERT_WEBHOOK_URL"):
        return False
    sender = _SENDERS.get(config.get("ALERT_TARGET_TYPE", "ntfy"))
    if sender is None:
        return False
    title, body = _build_message(cluster)
    sender(config["ALERT_WEBHOOK_URL"], title, body)
    return True


def apply_alert_throttle(client, clusters_index, cluster_key, rules, severity_weight, cluster_body, config):
    """Shared by app/dedup.py and app/anomaly.py: fires once when a
    cluster crosses into critical, not on every recurrence while it's
    still active. Returns the `alerted` value the caller should persist
    on the cluster doc it's about to write.

    A cluster that ages out (deleted by dedup's retention cleanup) and
    later reappears as a fresh burst has no prior doc, so this correctly
    treats it as a new occurrence and alerts again -- that's the point,
    not a bug.
    """
    if rules is None or rules.label_for_weight(severity_weight) != "critical":
        return False

    try:
        existing = client.get(index=clusters_index, id=cluster_key)
        alerted = bool(existing["_source"].get("alerted"))
    except NotFoundError:
        alerted = False

    if not alerted:
        try:
            send_alert(config, cluster_body)
        except Exception:
            pass  # best-effort -- a broken webhook target shouldn't break dedup/anomaly runs
    return True
