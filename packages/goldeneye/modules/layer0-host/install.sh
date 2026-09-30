#!/usr/bin/env bash
# layer0-host — host baseline (sysctl, ufw stubs, unattended-upgrades policy)
set -euo pipefail
KIT="${SHARED_SERVER_KIT_ROOT:-/opt/shared-server}"
# shellcheck source=/dev/null
source "$KIT/lib/common.sh"
ge_require_root
ge_step "layer0-host"

# inotify limits (Traefik/compose)
cat >/etc/sysctl.d/99-shared-server.conf <<'SYS'
fs.inotify.max_user_instances = 1024
fs.inotify.max_user_watches   = 1048576
SYS
sysctl --system >/dev/null 2>&1 || true

# SSH hardening snippet (does not replace distro defaults wholesale)
mkdir -p /etc/ssh/sshd_config.d
if [ ! -f /etc/ssh/sshd_config.d/50-shared-server.conf ]; then
  cat >/etc/ssh/sshd_config.d/50-shared-server.conf <<'SSH'
PasswordAuthentication no
KbdInteractiveAuthentication no
X11Forwarding no
ClientAliveInterval 30
ClientAliveCountMax 6
SSH
fi

# unattended-upgrades security-only preference (install package if present)
if command -v unattended-upgrade >/dev/null 2>&1 || dpkg -l unattended-upgrades >/dev/null 2>&1; then
  cat >/etc/apt/apt.conf.d/52shared_server-unattended <<'APT'
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
APT
fi

# ufw: only apply if already installed; do not lock operator out
if command -v ufw >/dev/null 2>&1; then
  ufw allow OpenSSH >/dev/null 2>&1 || true
  # tailscale interface if present
  if ip link show tailscale0 >/dev/null 2>&1; then
    ufw allow in on tailscale0 >/dev/null 2>&1 || true
  fi
  ufw allow 80/tcp >/dev/null 2>&1 || true
  ufw allow 443/tcp >/dev/null 2>&1 || true
  ge_info "ufw rules refreshed (enable manually with: ufw --force enable)"
fi

ge_info "layer0-host complete"
