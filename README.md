# prox-siem

A homelab SIEM for Proxmox VE + Proxmox Backup Server.

- **Phase 1 (done):** get real events flowing onto a dashboard as fast as
  possible, using Proxmox's own notification system instead of parsing logs.
- **Phase 2 (done):** raw logs (journal + task/access logs) via Fluent Bit,
  straight into the same OpenSearch index. See
  [`docs/log-shipping-setup.md`](docs/log-shipping-setup.md).
- **Phase 3 (done):** detection rules tag events at write time, a periodic
  job dedups bursts into clusters, and a ranking endpoint scores active
  clusters by severity x recency x frequency. See
  [`docs/detection-rules.md`](docs/detection-rules.md).
- **Phase 4 (done):** the UI became one coherent app -- **Overview**
  (ranked clusters, with host/category/time-range filters), **Explore**
  (free-text search + field filters across the raw stream), and
  **Pipeline Health** (per-source last-seen tracking, so a dead Fluent Bit
  agent or dropped Tailscale link shows up as "no data" instead of
  silently looking like "no problems"). Outbound alerting (ntfy by
  default, Discord/Gotify are just a different webhook target) fires once
  when a cluster first turns critical. ISM hot-tier retention is now a
  config value. See
  [`docs/phase4-operations.md`](docs/phase4-operations.md).
- **Phase 5 (done):** a second detection path -- volume-baseline anomaly
  detection -- flags a normally-quiet log type suddenly spiking, even
  with no line matching a known-bad pattern, and feeds it into the same
  clustering/ranking pipeline (tagged distinctly on Overview so you can
  tell rule-hits from anomaly-hits apart). A mute workflow lets you
  suppress a confirmed-benign `(rule, host)` pair from the dashboard
  instead of hand-editing `rules.yaml` -- it stays out of Overview while
  the raw events stay fully searchable in Explore. See
  [`docs/phase5-operations.md`](docs/phase5-operations.md).

Phase 1 deliberately skipped log parsing — Proxmox notifications already
arrive as structured, severity-tagged JSON for backup/replication/GC/sync/
cert events, which was enough to prove the whole pipe end-to-end first.
Phase 2 fills in everything the notification system doesn't cover (auth
failures, firewall drops, quorum/HA issues, kernel errors) by reading each
host's own journal and log files directly.

## How the pieces fit together

```
PVE / PBS (webhook notification) --POST--> ingestion API --\
PVE / PBS (Fluent Bit: journal, task logs, access log) ------+--> OpenSearch --query--> dashboard
```

**Why a webhook instead of tailing logs:** Proxmox's notification system
already classifies and structures the event (title, message, severity,
metadata fields) before it ever leaves the host. Reusing that means zero
parsing logic in Phase 1 — the ingestion API's only job is "trust the shape,
write it to an index."

**Why a separate ingestion API instead of pointing Proxmox straight at
OpenSearch:** Proxmox's webhook body template is Handlebars, not something
that can express OpenSearch's bulk/index API shape directly, and OpenSearch
here has no auth (see below) — the API is the one thing on the network
allowed to write, gated by a shared-secret header. It also gives a single
place to add real validation/auth/detection-rule scoring later without
touching Proxmox config again.

**Why OpenSearch:** it's the same engine the later Fluent Bit phase will
ship raw logs into, so the index/query patterns learned now (daily indices,
ISM lifecycle, mapping conventions) carry forward instead of getting thrown
away.

**Why the dashboard queries OpenSearch directly (no caching layer):** at
homelab volume this is a handful of events an hour — a raw `_search` sorted
by timestamp is plenty fast, and it's one less moving part to keep in sync.

## Layout

```
docker-compose.yml       OpenSearch, single node, bound to 127.0.0.1 + LAN IP
rules.yaml                Phase 3: detection rules (shared by both ingestion paths)
opensearch/               index template + ISM policy template + clusters index +
                          rules ingest pipeline (generated from rules.yaml) + setup.sh
app/
  ingest.py                POST /api/events — validates, tags, writes to OpenSearch
  dashboard.py              GET  /  (Overview), /explore (Explore), /top (redirect)
  rules_engine.py           loads rules.yaml, matches notification-path docs
  dedup.py                  burst-dedup: groups matched docs into clusters, fires alerts
  anomaly.py                Phase 5: volume-baseline anomaly detection, same clusters index
  mutes.py                  Phase 5: GET/POST /mutes — noise-suppression workflow
  ranking.py                GET /api/top-important — scores active clusters
  alerting.py               Phase 4: outbound webhook notifications (ntfy/Discord/Gotify)
  pipeline_health.py         GET /pipeline-health — per-source last-seen tracking
  templates/base.html        shared shell (nav/footer) — overview.html, explore.html,
                             pipeline_health.html, mutes.html all extend it
fluentbit/                Phase 2: Fluent Bit config, per host role (pve/pbs) + install.sh
                          (raw-log path tagged via the rules ingest pipeline; common.conf
                          also emits a Phase 4 per-host heartbeat)
scripts/run_dedup.py      standalone entrypoint for prox-siem-dedup.timer
scripts/run_anomaly.py    standalone entrypoint for prox-siem-anomaly.timer
scripts/test_phase3.py    end-to-end test: synthetic events -> tag -> dedup -> rank
docs/webhook-setup.md     Phase 1: exact steps for the PVE node + both PBS instances
docs/log-shipping-setup.md Phase 2: exact steps for installing Fluent Bit on each host
docs/detection-rules.md   Phase 3: rules.yaml syntax, dedup, ranking, testing
docs/phase4-operations.md Phase 4: Pipeline Health, alerting, ISM retention config
docs/phase5-operations.md Phase 5: anomaly tuning, mute workflow, how to extend this
prox-siem.service         optional systemd unit (gunicorn)
prox-siem-dedup.service/.timer    runs the dedup job every ~60s
prox-siem-anomaly.service/.timer  runs the anomaly job every ~1h
```

The ingestion API and dashboard are two Flask blueprints in one process —
they're conceptually separate but there's no reason to run two services for
this at homelab scale.

## Setup

```bash
# System prerequisite for OpenSearch (bootstrap check will fail without this)
sudo sysctl -w vm.max_map_count=262144
echo 'vm.max_map_count=262144' | sudo tee /etc/sysctl.d/99-opensearch.conf

# 1. Start OpenSearch
docker compose up -d

# 2. Create the index template + ISM policy
./opensearch/setup.sh

# 3. Configure the app
cp .env.example .env
# edit .env — set SHARED_SECRET to a long random value

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 4. Run it
.venv/bin/python run.py
# or in production: .venv/bin/gunicorn -w 2 -b 0.0.0.0:5000 run:app
# or install prox-siem.service (edit the User= and paths first)
```

Dashboard: `http://<vm-address>:5000/`

## Send a test event

Confirms the whole path — fake event → OpenSearch → dashboard — before
touching Proxmox config:

```bash
source .env
curl -s -X POST http://localhost:5000/api/events \
  -H 'Content-Type: application/json' \
  -H "X-Prox-Siem-Secret: $SHARED_SECRET" \
  -d '{
    "timestamp": '"$(date +%s)"',
    "severity": "warning",
    "title": "Test event",
    "message": "Manual test event to confirm ingest -> OpenSearch -> dashboard works.",
    "fields": { "type": "test", "hostname": "test-host" }
  }'
```

Then reload the dashboard — you should see it as the newest row with a
yellow "warning" badge.

Once that works, follow [`docs/webhook-setup.md`](docs/webhook-setup.md) to
point the PVE node and both PBS instances at this API for real.
