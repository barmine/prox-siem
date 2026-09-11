# Phase 5: anomaly detection + noise suppression

Two additions on top of Phase 3's pattern-rule detection: a second,
statistical detection path for things no regex can catch, and a way to
permanently quiet a confirmed-benign recurring match instead of it
piling up on Overview forever.

## Volume-baseline anomaly detection (`app/anomaly.py`, `prox-siem-anomaly.timer`)

Runs hourly, independently of the dedup job, same reasoning as dedup
itself (a scheduler running in-process inside gunicorn's multiple
workers would double-fire). For each `(hostname, unit)` pair seen in the
last `ANOMALY_BASELINE_DAYS` (default 14) days, it buckets hourly counts,
treats every bucket except the most recent (possibly partial) hour as
the baseline, and flags the current hour if it's a statistically
significant deviation:

- **z-score path** (baseline has variance): `z = (current - mean) / stdev`.
  `z >= ANOMALY_ZSCORE_THRESHOLD` (default 3.0) -> warning,
  `z >= ANOMALY_ZSCORE_CRITICAL` (default 6.0) -> critical.
- **Fallback path** (zero-variance baseline -- a normally-silent source,
  where z-score is undefined): flags if
  `current >= max(ANOMALY_MIN_COUNT, mean * ANOMALY_MULTIPLIER_FALLBACK)`.
  This is the spec's own example -- "a normally-quiet log type suddenly
  spiking" -- and z-score alone can't express it when stdev is 0.
- `ANOMALY_MIN_COUNT` (default 5) is an absolute floor regardless of
  z-score -- a source going from 1 event/hour to 3 is statistically loud
  but operationally meaningless.

Anomalies write into the same `proxmox-clusters` index Phase 3's dedup
job uses, with `rule_id: "anomaly:<unit>"` and `detection_method:
"anomaly"` (pattern-rule clusters now carry `detection_method: "rule"`
for symmetry). The synthetic rule_id is deliberate: the mute system
below works identically for both detection methods with no
special-casing, and Overview needed no rendering changes to display
anomaly cards -- they're clusters like any other, just tagged
distinctly (an "anomaly" badge on the card, and a detection-method
filter chip once both kinds exist).

**Known limitation**: `proxmox-logs-*` indices created before `unit` was
consistently mapped as `keyword` in the index template (the very first
few days of Phase 2) have it as `text` instead, which OpenSearch refuses
to aggregate on (`multi_terms`/`terms` throw a per-shard
`illegal_argument_exception`). The anomaly query still returns partial
results from the unaffected shards rather than failing outright, so this
degrades baseline accuracy for sources with history in one of those old
indices rather than crashing -- and it's self-healing: once an affected
index ages past `ANOMALY_BASELINE_DAYS`, it drops out of the lookback
window on its own. Not worth a reindex to fix a few days of
already-transient inaccuracy.

**Tuning**: if it's too noisy, raise `ANOMALY_ZSCORE_THRESHOLD` and/or
`ANOMALY_MIN_COUNT`. If a specific normally-spiky source (e.g. a backup
job's access-log burst) keeps false-triggering, mute it rather than
loosening the thresholds for everything else (see below) -- its
synthetic rule_id is `anomaly:<unit>`.

## Noise suppression / mutes (`app/mutes.py`, `/mutes`)

Mute a `(rule_id, hostname)` pair -- or a `rule_id` for every host, leave
hostname blank -- once you've confirmed a recurring match is benign.
Two ways to create one:
- **Quick action**: the "mute on `<hostname>`" button on any Overview
  cluster card (always host-scoped -- the safer, more common case).
- **`/mutes` page**: for muting ad-hoc (no existing cluster needed) or
  for an all-hosts mute.

**What muting actually does**: `app/dedup.py` and `app/anomaly.py` each
check the active mute list once per run, before writing anything.
A muted match is dropped from clustering entirely -- if it already had a
cluster, that's deleted immediately (not left to passively age out).
**Raw events keep their `rule_id`/`category` tags regardless** -- tagging
happens at write time (the ingest pipeline / `app/ingest.py`), completely
independent of muting -- so Explore's full history is unaffected; only
the clustered/ranked Overview view respects mutes. This is deliberate:
muting is about keeping the top-of-list trustworthy, not erasing the
record of what happened.

Unmuting (the "Unmute" button on `/mutes`) takes effect on the next
dedup/anomaly run -- the match starts clustering again immediately if
there's still matching activity.

## Extending this

This phase is meant to repeat -- here's the loop for each type of change:

**Add a new pattern rule**: edit `rules.yaml` (see the syntax comment at
its top, or `docs/detection-rules.md`), then
`./opensearch/setup.sh` on the SIEM VM to regenerate the Fluent Bit
ingest pipeline. No code or restart needed for the notification-webhook
path (`app/rules_engine.py` re-reads `rules.yaml` on every Flask
restart) -- restart `prox-siem.service` to pick it up there too.

**Mute a new false positive**: from the Overview card once you've seen
it happen and confirmed it's benign, or via `/mutes` directly if you
already know the `rule_id` (check `rules.yaml` for pattern-rule ids, or
`anomaly:<unit>` for a volume-anomaly source -- the `unit` value is
visible on the anomaly's Overview card and in Explore).

**Adjust anomaly sensitivity**: tune `ANOMALY_ZSCORE_THRESHOLD` /
`ANOMALY_ZSCORE_CRITICAL` / `ANOMALY_MIN_COUNT` /
`ANOMALY_MULTIPLIER_FALLBACK` in `.env` and restart
`prox-siem-anomaly.timer`'s next run picks it up automatically (no
restart needed -- `scripts/run_anomaly.py` reads `.env` fresh each
invocation). Prefer muting one noisy source over loosening thresholds
globally, unless the noise is genuinely widespread.
