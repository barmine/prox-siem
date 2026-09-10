# Phase 3: detection rules, dedup, and ranking

Turns the flat `proxmox-logs-*` stream into a ranked "what actually needs
attention right now" list, without re-scanning raw text on every dashboard
load.

```
proxmox-logs-* write  --(Python match, app/rules_engine.py)-->  tagged doc   [notification path]
proxmox-logs-* write  --(ingest pipeline, generated from rules.yaml)-->  tagged doc   [Fluent Bit path]
                                        |
                                        v
                     rule_id / category / severity_weight on the doc
                                        |
                    prox-siem-dedup.timer, every ~60s (app/dedup.py)
                                        v
                         proxmox-clusters (one doc per burst)
                                        |
                    GET /api/top-important  /  dashboard "Top Important" tab
                              (app/ranking.py, app/dashboard.py)
```

## rules.yaml

One file, both paths. See the comment block at the top of
[`rules.yaml`](../rules.yaml) for the exact match syntax
(`equals` / `not_equals` / `regex`, dotted `field` paths into `fields.*`,
first-match-wins ordering). To add a rule: add an entry, then re-run
`./opensearch/setup.sh` on the SIEM VM to regenerate the ingest pipeline --
no application code changes needed. The Flask app re-reads rules.yaml on
every restart (`app/rules_engine.py`); it is not currently hot-reloaded
while the process is running.

**Why two engines for one file:** the notification-webhook path goes
through Flask, so it can just call into `app/rules_engine.py` directly
before indexing (`app/ingest.py`). The Fluent Bit raw-log path writes
straight to OpenSearch and never touches Flask, so the same rules.yaml is
compiled into a Painless `script` processor and installed as the
`proxmox-logs-rules` ingest pipeline (`opensearch/generate_rules_pipeline.py`);
Fluent Bit's output config opts into it with `Pipeline proxmox-logs-rules`
(`fluentbit/conf.d/output-opensearch.conf`). Python's `re` and Java's
`Pattern`/`Matcher` are close enough for the simple patterns rules.yaml
uses that the same regex string works unmodified in both engines --
that's why the pattern syntax is kept deliberately simple.

**Seed rules are approximations.** In particular, the notification-path
rules (backup/replication/GC/ACME) match on `title`/`message` text rather
than `fields.type`, because `fields.type`'s real values haven't been
confirmed against a live Proxmox webhook payload yet (same caveat as
`docs/webhook-setup.md`). Tighten them once real payloads have been seen.

## Dedup (`app/dedup.py`, `prox-siem-dedup.timer`)

Runs as a **separate systemd timer**, not an in-process scheduler --
gunicorn runs multiple worker processes (`-w 2`), and an in-process
scheduler would fire once per worker and double-count everything.

Each run recomputes clusters from scratch over a rolling lookback window
(`DEDUP_LOOKBACK_MINUTES`, default 30) rather than keeping a cursor: no
state to get out of sync, and a cluster's `count`/`first_seen`/`last_seen`
describe "within the last N minutes", which is what "what's wrong right
now" needs. Grouping key: `rule_id` + `hostname` + a normalized message
(digits/IPs collapsed to `#`, so 500 firewall drops differing only by
source port land in one bucket). Raw docs get tagged with the resulting
`cluster_key` for exact drill-down; clusters not refreshed by a run in
`DEDUP_RETENTION_MINUTES` (default 120) get deleted as resolved.

Install once on the SIEM VM (edit `User=`/paths first, same as
`prox-siem.service`):

```bash
sudo cp prox-siem-dedup.service prox-siem-dedup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now prox-siem-dedup.timer
```

## Ranking (`app/ranking.py`)

`score = severity_weight * recency * log(count + 1)`, where recency is an
exponential decay with a half-life of `RANKING_HALF_LIFE_MINUTES` (default
60) applied to a cluster's `last_seen`. The `+ 1` matters: a single
critical event (`count == 1`) would otherwise score exactly zero
(`log(1) == 0`) and vanish under any high-frequency noise regardless of
severity -- the opposite of the point of this phase.

Only clusters seen within `RANKING_ACTIVE_WINDOW_HOURS` (default 24) count
as "active". `GET /api/top-important?limit=50` returns the ranked JSON;
the dashboard's "Top Important" tab calls the same `get_top_clusters()`
function directly rather than hitting its own HTTP endpoint.

## Testing end-to-end

```bash
.venv/bin/python scripts/test_phase3.py           # leaves synthetic docs in place
.venv/bin/python scripts/test_phase3.py --cleanup  # deletes them afterward
```

Sends a fake failed-backup notification through the real Flask API, a
fake ZFS-degraded line, 8 fake auth failures, and 50 identical firewall
drops directly through the `proxmox-logs-rules` ingest pipeline, runs the
dedup job once inline (no need to wait for the timer), and checks: each
line got the right `rule_id`/`category`/`severity_weight`, the auth
failures collapsed into one 8-count cluster and the firewall drops into
one 50-count cluster, and the ranking puts the single critical ZFS event
and the frequent critical auth-failure cluster both above the
high-frequency-but-low-severity firewall cluster.
