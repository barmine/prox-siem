# Phase 2: shipping raw logs with Fluent Bit

Phase 1 only sees what Proxmox's notification system already structures
and decides to send. Phase 2 adds a second, parallel pipeline: Fluent Bit
running natively on each host, reading the systemd journal and a couple of
log files directly, writing straight into the same `proxmox-logs-*` index
pattern OpenSearch already has a template + ISM policy for.

```
PVE node   --Fluent Bit--\
local PBS  --Fluent Bit---+--> OpenSearch (proxmox-logs-*) <-- unchanged from Phase 1
                                     ^
                                     |
              Phase 1 ingestion API-+  (Proxmox webhook notifications, unchanged)
```

Two producers, one index pattern, one dashboard. Nothing from Phase 1
changes except: OpenSearch is now reachable from more than just
`127.0.0.1` (see below), and the index template gained one field (`unit`).

**Scope right now: PVE node + local PBS only.** The remote VPS PBS over
Tailscale is deferred -- the `fluentbit/` config and `install.sh` already
support it (`--role pbs --siem-host <tailscale-ip>`), and the Tailscale
ACL-check procedure is written up below for when it's picked back up, but
nothing on the remote VPS should be touched yet.

## Why this needed a network change first

Phase 1 reasoned "OpenSearch has no auth because it's never reachable
outside loopback." Phase 2 breaks that assumption on purpose -- remote
hosts now need to write to it directly. Rather than bolt on OpenSearch
security-plugin auth (certs, users, roles -- a bigger lift than this phase
calls for), the SIEM VM now restricts access at the network layer instead:

- OpenSearch (`:9200`) is bound to `127.0.0.1` (local ingestion API) and
  the LAN IP `192.168.50.248` (PVE node + local PBS). A Tailscale IP
  binding gets added later, when the remote PBS work resumes.
- `ufw` is now active on the SIEM VM, default-deny incoming, with explicit
  allows for 22 (SSH), and 9200 + 5000 from `192.168.50.0/24`.
- OpenSearch itself still has no application-layer auth -- the trust
  boundary is "you're on the LAN," same posture as Phase 1 had for
  loopback. Worth revisiting if this ever leaves a homelab context.

## Prerequisites

- `opensearch/index_template.json` was updated to add a `unit` keyword
  field; re-run `./opensearch/setup.sh` on the SIEM VM if you haven't since
  this changed (safe to re-run -- see the script's own idempotency notes).

## Install on the PVE node

Copy the `fluentbit/` directory from this repo to the PVE node (`scp -r`,
or `git clone` if the node has network access to GitHub), then:

```bash
cd fluentbit
sudo ./install.sh --role pve --siem-host 192.168.50.248
```

This adds Fluent Bit's official apt repo, installs the package, drops in
the PVE-role config, and starts it via systemd (enabled on boot).

## Install on the local PBS instance

Same repo, same SIEM host (it's on the same LAN):

```bash
cd fluentbit
sudo ./install.sh --role pbs --siem-host 192.168.50.248
```

## Install on the remote VPS PBS instance (deferred -- do not run yet)

Kept here for when this gets picked back up; skip this section for now.

**Do this check first, before installing anything** -- there's a known
history on this network of Tailscale ACL rules silently blocking traffic
between the two PBS instances, and this new SIEM VM is a new node on the
tailnet that hasn't been exercised yet. Don't assume it can reach the SIEM
VM just because both are "on Tailscale."

1. On the SIEM VM, get its Tailscale IP and confirm the VPS shows up as a peer:
   ```bash
   tailscale ip -4
   sudo tailscale status   # the VPS PBS should appear here, not "offline"
   ```
2. On the remote VPS PBS, confirm it can reach the SIEM VM's Tailscale IP
   at the protocol level, not just ping:
   ```bash
   tailscale ping <siem-vm-tailscale-ip>
   curl -v --max-time 5 http://<siem-vm-tailscale-ip>:9200/
   ```
   `curl` needs to return OpenSearch's JSON banner (cluster name, version).
   A `tailscale ping` success with a `curl` timeout/connection-refused is
   exactly the ACL-blocking-a-specific-port symptom this network has hit
   before -- if you see that split, check the ACL policy in the Tailscale
   admin console for rules scoping which ports/tags can reach the SIEM VM
   node, before touching the Fluent Bit config at all.
3. Once step 2's `curl` succeeds:
   ```bash
   cd fluentbit
   sudo ./install.sh --role pbs --siem-host <siem-vm-tailscale-ip>
   ```

## Verify the parsers before trusting them

`fluentbit/parsers.conf` includes two regex parsers (`pve_task_index`,
`pbs_task_active`) built from Proxmox's documented UPID format, but **not
verified against a live host in this session**. Before assuming they work:

```bash
# on the PVE node
tail -f /var/log/pve/tasks/index
# trigger something that creates a task line (e.g. a manual backup), and
# compare the real line against parsers.conf's pve_task_index regex

# on a PBS instance
tail -f /var/log/proxmox-backup/tasks/active
```

If a line doesn't match, Fluent Bit doesn't drop it -- it ships the raw
line under a `log` field instead of the decomposed fields, so nothing is
silently lost, but you won't get structured `worker_type`/`status`/etc.
Fix the regex in `parsers.conf`, redeploy (`install.sh` again, or just
`scp` the updated file + `systemctl restart fluent-bit`), and re-check.

Also worth a quick look: `sshd`'s systemd unit name. Both configs assume
`ssh.service` (the Debian/Proxmox convention). Confirm with:
```bash
systemctl status ssh
```
If it's actually named differently on your version, edit the
`_SYSTEMD_UNIT=ssh.service` line in `conf.d/input-pve.conf` /
`conf.d/input-pbs.conf` before installing.

## End-to-end verification

Do this on **each** host role after installing.

1. **Journal path** -- trigger an SSH auth failure (harmless, from another
   machine): `ssh baduser@<host> ` and enter a wrong password, or just
   `ssh -o PreferredAuthentications=password baduser@<host>` and let it fail.
2. Confirm Fluent Bit picked it up locally:
   ```bash
   journalctl -u fluent-bit -n 50 --no-pager   # no errors shipping to OpenSearch
   ```
3. Confirm it landed in OpenSearch, on the SIEM VM:
   ```bash
   curl -s "http://localhost:9200/proxmox-logs-*/_search?q=unit:ssh.service&sort=timestamp:desc&size=1&pretty"
   ```
   Check the hit has `severity`, `hostname` (matching the host you tested
   on), `unit: ssh.service`, `source_type: journal`, and a `message` that
   contains the failed-login text.
4. Confirm it renders on the dashboard: `http://192.168.50.248:5000/` --
   look for the new row (it'll have a blank `title`, since raw log events
   don't have Proxmox's notification title -- that's expected, Phase 3
   territory).
5. Repeat on both roles (PVE, local PBS) -- each should show its own
   `hostname` and confirm the whole chain (journal -> Fluent Bit -> network
   path -> OpenSearch -> dashboard) works from that specific host, not just
   one of them. (Add the remote PBS to this check once that part resumes.)

For the task-log and access-log paths specifically, trigger something
concrete rather than waiting:
- PVE: run a manual backup or `vzdump` on any guest, then check for a
  `source_type: task-log` hit with `unit` set to the job type.
- PVE: load any page in the PVE web UI, then check for a
  `source_type: access-log` hit.
- PBS: run a manual prune/GC/backup job, then check for a
  `source_type: task-log` hit on the PBS host.
