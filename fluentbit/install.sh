#!/usr/bin/env bash
# Installs Fluent Bit as a native apt package and configures it for one
# Proxmox host role. Run on the PVE node, the local PBS instance, and the
# remote VPS PBS instance -- once each, with the right --role/--siem-host.
#
# Usage:
#   sudo ./install.sh --role pve --siem-host 192.168.50.248
#   sudo ./install.sh --role pbs --siem-host 192.168.50.248        # local PBS
#   sudo ./install.sh --role pbs --siem-host 100.x.x.x             # remote PBS, Tailscale IP
set -euo pipefail

ROLE=""
SIEM_HOST=""
SIEM_PORT="9200"

while [ $# -gt 0 ]; do
  case "$1" in
    --role) ROLE="$2"; shift 2 ;;
    --siem-host) SIEM_HOST="$2"; shift 2 ;;
    --siem-port) SIEM_PORT="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [ "$ROLE" != "pve" ] && [ "$ROLE" != "pbs" ]; then
  echo "Usage: $0 --role pve|pbs --siem-host <ip> [--siem-port 9200]" >&2
  exit 1
fi
if [ -z "$SIEM_HOST" ]; then
  echo "Usage: $0 --role pve|pbs --siem-host <ip> [--siem-port 9200]" >&2
  exit 1
fi
if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root (sudo)." >&2
  exit 1
fi

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODENAME="$(. /etc/os-release && echo "$VERSION_CODENAME")"

echo "==> Adding Fluent Bit apt repo (debian/${CODENAME})"
curl -fsSL https://packages.fluentbit.io/fluentbit.key | gpg --dearmor > /usr/share/keyrings/fluentbit-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/fluentbit-keyring.gpg] https://packages.fluentbit.io/debian/${CODENAME} ${CODENAME} main" \
  > /etc/apt/sources.list.d/fluent-bit.list
apt-get update
apt-get install -y fluent-bit

echo "==> Installing config for role: ${ROLE}"
mkdir -p /etc/fluent-bit/conf.d /var/lib/fluent-bit/storage
cp "${DIR}/parsers.conf" /etc/fluent-bit/parsers.conf
cp "${DIR}/filters.lua" /etc/fluent-bit/filters.lua
cp "${DIR}/conf.d/common.conf" /etc/fluent-bit/conf.d/common.conf
cp "${DIR}/conf.d/output-opensearch.conf" /etc/fluent-bit/conf.d/output-opensearch.conf
cp "${DIR}/conf.d/input-${ROLE}.conf" /etc/fluent-bit/conf.d/input-${ROLE}.conf
cp "${DIR}/fluent-bit-${ROLE}.conf" /etc/fluent-bit/fluent-bit.conf

echo "==> Writing /etc/default/fluent-bit"
cat > /etc/default/fluent-bit <<EOF
SIEM_OPENSEARCH_HOST=${SIEM_HOST}
SIEM_OPENSEARCH_PORT=${SIEM_PORT}
EOF

echo "==> Installing systemd override (FB_HOSTNAME, EnvironmentFile)"
mkdir -p /etc/systemd/system/fluent-bit.service.d
cp "${DIR}/fluent-bit.service.d-override.conf" /etc/systemd/system/fluent-bit.service.d/override.conf

echo "==> Enabling and starting fluent-bit"
systemctl daemon-reload
systemctl enable --now fluent-bit
sleep 2
systemctl status fluent-bit --no-pager || true

echo
echo "Done. Check 'journalctl -u fluent-bit -f' for shipping errors, and see"
echo "docs/log-shipping-setup.md for the end-to-end verification steps."
