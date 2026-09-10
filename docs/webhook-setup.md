# Configuring Proxmox webhook targets

Do this on the PVE node and on **both** PBS instances. The steps and values
are identical everywhere except the URL for the remote PBS (over Tailscale).

Proxmox's webhook notification target lets you template the `url`, `header`,
and `body` fields with Handlebars, and reference protected values via a
separate `secret` store (`{{ secrets.<name> }}`) so the shared secret never
sits in plain text in the body/header template itself.

## 0. Prerequisites

- The shared secret value: whatever you set `SHARED_SECRET` to in `.env` on
  the prox-siem VM.
- The prox-siem VM's address as reachable from each Proxmox host:
  - From the PVE node and the **local** PBS instance: the VM's normal LAN IP,
    e.g. `http://10.x.x.x:5000/api/events`.
  - From the **remote** PBS instance: the VM's **Tailscale** IP/hostname,
    e.g. `http://100.x.x.x:5000/api/events`, since the remote PBS can't
    reach the LAN directly.

## 1. Add the webhook endpoint (GUI)

On each host: **Datacenter → Notifications → Add → Webhook** (PVE) or
**Configuration → Notifications → Add → Webhook** (PBS).

| Field | Value |
|---|---|
| Name | `prox-siem` |
| URL | `http://<vm-address>:5000/api/events` |
| Method | `POST` |
| Header 1 | Name: `Content-Type`, Value: `application/json` |
| Header 2 | Name: `X-Prox-Siem-Secret`, Value: `{{ secrets.shared_secret }}` |
| Body | see below |

**Don't skip this part** — it's easy to miss because it's a separate
section of the same dialog, below Headers, not another header row: add a
**Secret** with Name `shared_secret`, Value `<your SHARED_SECRET value>`
(the same string as `SHARED_SECRET` in `.env` on the SIEM VM). Without it,
`{{ secrets.shared_secret }}` in Header 2 above has nothing to resolve to,
so the `X-Prox-Siem-Secret` header goes out empty and the ingestion API
rejects the request with 401.

Body (Handlebars template — paste exactly, `escape` guards against control
characters/quotes breaking the JSON):

```
{
  "timestamp": {{ timestamp }},
  "severity": "{{ escape severity }}",
  "title": "{{ escape title }}",
  "message": "{{ escape message }}",
  "fields": {
    "type": "{{#if fields.type}}{{escape fields.type}}{{/if}}",
    "hostname": "{{#if fields.hostname}}{{escape fields.hostname}}{{/if}}"
  }
}
```

This matches what `app/ingest.py` expects: `timestamp` (epoch seconds) and
`severity` are required; everything else is optional. `fields.type` and
`fields.hostname` are the metadata keys Proxmox populates for most event
types (backup job, replication, GC, sync, cert renewal, etc.) — if a
specific event doesn't set one, it just renders empty and the ingestion API
falls back to `"unknown"`.

The `{{#if fields.type}}...{{/if}}` wrapping is required, not stylistic:
Proxmox's webhook templating runs Handlebars in strict mode, where passing
a path that doesn't resolve (e.g. `fields.type` when no `fields.type` key
exists in the notification's context) directly as a helper argument —
`{{ escape fields.type }}` — throws instead of rendering empty. The
`#if` block checks the path without that restriction, so it only calls
`escape` when the key is actually present. This matters most for the
Proxmox GUI's **Test** button, whose synthetic notification doesn't set
`fields.type`/`fields.hostname` at all — without the `#if` guard, Test
always fails with `Helper/Decorator escape param at index 0 required but
not found`, even though real events (which do set those keys) render
fine.

## 2. Add a catch-all matcher (GUI)

**Datacenter/Configuration → Notifications → Add → Matcher**

| Field | Value |
|---|---|
| Name | `prox-siem-catchall` |
| Target | `prox-siem` |
| Match rules | *(leave empty — no conditions = matches everything)* |

A matcher with no filter rules always matches, so every notification
(any severity, any type) gets routed to the webhook. That's deliberate for
Phase 1: no filtering/scoring yet, just get everything flowing into
OpenSearch so the dashboard has real data to look at.

## 3. CLI alternative (PVE only, via `pvesh`)

Equivalent to the GUI steps above, if you'd rather script it:

```bash
pvesh create /cluster/notifications/endpoints/webhook \
  --name prox-siem \
  --url 'http://<vm-address>:5000/api/events' \
  --method POST \
  --header '[{"name":"Content-Type","value":"application/json"},{"name":"X-Prox-Siem-Secret","value":"{{ secrets.shared_secret }}"}]' \
  --secret '[{"name":"shared_secret","value":"<your SHARED_SECRET value>"}]' \
  --body '{"timestamp": {{ timestamp }}, "severity": "{{ escape severity }}", "title": "{{ escape title }}", "message": "{{ escape message }}", "fields": {"type": "{{#if fields.type}}{{escape fields.type}}{{/if}}", "hostname": "{{#if fields.hostname}}{{escape fields.hostname}}{{/if}}"}}'

pvesh create /cluster/notifications/matchers \
  --name prox-siem-catchall \
  --target prox-siem
```

PBS uses the equivalent `proxmox-backup-manager notifications` subcommands
on each PBS host; the GUI path is more reliable there, so use step 1/2 for
both PBS instances.

## 4. Verify

- Trigger something cheap, e.g. manually run a backup job or GC on one
  datastore, and watch for a new row on the prox-siem dashboard.
- If nothing shows up: check **Datacenter/Configuration → Notifications →
  Webhook target → "Test"** in the Proxmox GUI (sends a sample event through
  the same path), and check `journalctl -u prox-siem -f` (or the terminal
  running `flask run`) on the VM for the HTTP request/response.
