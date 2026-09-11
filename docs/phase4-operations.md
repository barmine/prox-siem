# Phase 4: daily-use dashboard + pipeline trust

Turns the two bolted-together pages from Phase 3 into one coherent app --
**Overview** (ranked clusters, was "Top Important"), **Explore** (raw
search, was the plain event list), and **Pipeline Health** (new) -- and
adds the two things needed to actually trust it day to day: knowing when
the pipeline itself has gone quiet, and getting pushed a notification the
first time something turns critical.

## Overview (`/`)

Same ranking as before (severity x recency x log(frequency)), now with:

- **Host and category filters** -- chip lists, same style as Explore's
  host filter.
- **Time range**: `1h` / `24h` / `7d` preset chips, or `custom` with
  `since`/`until` (UTC, matches every other timestamp in the app --
  there's no per-user timezone concept anywhere else here). A swapped
  range (`since` after `until`) is validated and surfaced as an explicit
  error rather than silently returning zero results -- `gte > lte` would
  otherwise just never match anything in OpenSearch, which reads as "no
  clusters" instead of "this range can't work."

**Why `DEDUP_RETENTION_MINUTES` changed from 2h to 7d default**: clusters
are hard-deleted from `proxmox-clusters` once they've been quiet longer
than this. A `7d` or custom range picker is pointless if the underlying
cluster docs don't survive that long -- so Phase 4 bumps the default to
match the widest preset. This is independent of `DEDUP_LOOKBACK_MINUTES`
(still 30m), which controls what counts as "currently active" for
count/first_seen/last_seen, not how long a *resolved* cluster stays
browsable.

## Explore (`/explore`)

The old raw event list, now with free-text search (`multi_match` across
`title`/`message`) plus `unit` and `source_type` filters alongside the
existing severity/host/cluster ones. Severity counts respect whatever
else is currently filtered/searched (excluding severity itself, so
picking one severity doesn't hide the others); host/unit/source_type stay
plain global chip lists, same as host filtering already worked.

`source_type: heartbeat` (see below) is hidden by default -- it's
pipeline plumbing, not something you want cluttering a raw browse.
Filter for it explicitly if you want to check it.

## Pipeline Health (`/pipeline-health`)

Tracks a last-seen timestamp per ingestion source and flags anything
quieter than expected for that *kind* of source:

- **Each Fluent-Bit-shipping host** (PVE node, each PBS instance): a
  `dummy` Fluent Bit input (`fluentbit/conf.d/common.conf`) emits a
  heartbeat record every 60s, tagged `source_type: heartbeat`, written
  into the same `proxmox-logs-*` index as everything else. This answers
  "is the agent alive and reaching OpenSearch" directly -- inferring
  liveness from real log volume doesn't work, because a quiet host doing
  nothing legitimate looks identical to a dead agent. Flagged stale after
  `PIPELINE_HEARTBEAT_STALE_MINUTES` (default 5 -- five missed heartbeats
  in a row is a real problem).
- **The notification-webhook path** (one source, not per-host --
  Proxmox notifications can come from any host, but what matters is
  whether the path itself is still receiving anything): flagged stale
  after `PIPELINE_WEBHOOK_STALE_HOURS` (default 48). This is much longer
  than the heartbeat threshold on purpose -- Proxmox only sends a
  notification when something actually happens, so quiet here can be
  entirely normal (a quiet GC job's notification path for a day doesn't
  mean anything is broken).

**Requires redeploying `fluentbit/conf.d/common.conf` to every host**
(same `install.sh`-or-manual-patch process used for every Fluent Bit
config change so far -- copy the file, `systemctl restart fluent-bit`).
Until that's done on a given host, it simply won't appear on this page
(no heartbeat docs yet), not show as stale.

## Outbound alerting (`app/alerting.py`, hooked into `app/dedup.py`)

Fires once when a cluster crosses into **critical** severity for the
first time -- not on every dedup run while it's still active, and not
again if it recurs after aging out and coming back (that's treated as a
new occurrence, which is correct: it's a fresh alert-worthy event).

Disabled by default. To enable:

```bash
ALERT_ENABLED=true
ALERT_TARGET_TYPE=ntfy        # or discord, gotify
ALERT_WEBHOOK_URL=https://ntfy.sh/your-topic-here   # or self-hosted
```

`ALERT_WEBHOOK_URL` is always the *full* target URL:
- **ntfy**: your topic URL (`https://ntfy.sh/<topic>` or self-hosted
  equivalent).
- **discord**: the channel's webhook URL.
- **gotify**: `https://your-gotify-host/message?token=<apptoken>`.

Swapping targets is a config change, not a code change. Adding a new
target type later means one more small function in `app/alerting.py`
plus a dispatch-table entry -- see `_SENDERS` there.

The throttle state (`alerted: true/false`) lives directly on the cluster
doc in `proxmox-clusters`, so it needs no extra infrastructure and
survives fine across dedup runs.

## ISM retention (`opensearch/ism_policy.json.template`, `opensearch/setup.sh`)

Hot-tier retention is now a `setup.sh`-time variable instead of hardcoded
in the policy JSON:

```bash
ISM_HOT_RETENTION_DAYS=90 ./opensearch/setup.sh
```

**Changing it on an already-bootstrapped cluster**: OpenSearch ISM
policies use optimistic concurrency control, so a plain PUT can't
blindly overwrite a policy that already exists with different content --
you'll get a `version_conflict`. Delete it first, then re-run:

```bash
curl -X DELETE "${OPENSEARCH_URL}/_plugins/_ism/policies/proxmox-logs-policy"
ISM_HOT_RETENTION_DAYS=90 ./opensearch/setup.sh
```

**Disk projections**, from real numbers measured on the live SIEM VM
during this phase's rollout (`proxmox-logs-2026.09.09`, one full day:
49,041 docs / 5.5MB; `proxmox-logs-2026.09.10`, partial day post-fix:
83,675 docs / 7.9MB and climbing -- call it ~10MB/day as the current,
slightly conservative rate):

| Retention | Approx. disk |
|---|---|
| 7 days | ~70MB |
| 30 days (default) | ~300MB |
| 90 days | ~900MB |

All trivial against this VM's 24GB disk (15GB free at time of writing).
Given the footprint, 90 days is very affordable if you want more
history than the current 30-day default -- that's a policy call, not a
technical constraint.
