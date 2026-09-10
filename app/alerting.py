"""Phase 4: outbound alerting. Fires when a cluster crosses into critical
severity for the first time -- the "first time" check lives in
app/dedup.py's run_once() (it knows the previous state of the cluster
doc), this module only knows how to build and send one notification.

ALERT_TARGET_TYPE picks the payload shape; ALERT_WEBHOOK_URL is always the
*full* target URL (an ntfy topic URL, a Discord webhook URL, a Gotify
".../message?token=..." URL). Swapping targets is a config change, not a
code change -- adding a new target type later is one more small function
plus a dispatch-table entry.
"""
import json
import urllib.request


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
