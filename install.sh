#!/bin/bash
# Root install with two users: `workq` runs the queue and holds the git key,
# `workq-agent` runs Claude and holds only the Claude token.
# For Ubuntu or Debian (apt) and RHEL 9 or its rebuilds (dnf). Run from the unzipped folder:
#   sudo ./install.sh
# Safe to run again: it never overwrites your config, credentials or keys.
# No root access? Use ./install-user.sh instead.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
PREFIX=/opt/workq
ETC=/etc/workq
STATE=/var/lib/workq

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo ./install.sh" >&2
  exit 1
fi

echo "== packages"
if command -v apt-get >/dev/null; then
  apt-get update -qq
  apt-get install -y -qq git openssh-client python3 python3-yaml sudo msmtp-mta >/dev/null || \
    apt-get install -y -qq git openssh-client python3 python3-yaml sudo msmtp >/dev/null
elif command -v dnf >/dev/null; then
  # msmtp is not in the RHEL repos: set digest.smtp in the config, or point
  # digest.send_command at the mail program your site uses.
  dnf install -y -q git openssh-clients python3 sudo
else
  echo "Neither apt-get nor dnf found. Install git, python3 (3.9+), sudo and ssh-keygen yourself, then rerun." >&2
  exit 1
fi

echo "== users"
getent group workq >/dev/null || groupadd --system workq
id workq >/dev/null 2>&1 || useradd --system --create-home --gid workq --shell /bin/bash workq
id workq-agent >/dev/null 2>&1 || useradd --system --create-home --gid workq --shell /bin/bash workq-agent
chmod 700 /home/workq
chmod 700 /home/workq-agent

echo "== program files"
install -d -m 755 "$PREFIX" "$PREFIX/bin"
install -m 755 "$SRC/bin/workq" "$PREFIX/bin/workq"
install -m 755 "$SRC/bin/agent-claude" "$PREFIX/bin/agent-claude"
rm -rf "$PREFIX/template" "$PREFIX/lib"
cp -r "$SRC/template" "$PREFIX/template"
cp -r "$SRC/lib" "$PREFIX/lib"
chmod -R a+rX "$PREFIX"
ln -sf "$PREFIX/bin/workq" /usr/local/bin/workq

echo "== config"
install -d -m 755 "$ETC"
if [[ ! -f "$ETC/config.yaml" ]]; then
  install -m 640 -o root -g workq "$SRC/etc/config.example.yaml" "$ETC/config.yaml"
  echo "   wrote $ETC/config.yaml: edit it before first use"
fi
if [[ ! -f "$ETC/agent.env" ]]; then
  install -m 600 -o workq-agent -g workq "$SRC/etc/agent.env.example" "$ETC/agent.env"
  echo "   wrote $ETC/agent.env: add the Claude token"
fi

echo "== state folder"
# The agent user (in group workq) may pass through to the one clone the runner opens
# for it during a run. It cannot list or write anything else here.
install -d -m 0710 -o workq -g workq "$STATE"

echo "== sudo rule (runner may start Claude as workq-agent, nothing else)"
install -m 440 "$SRC/etc/sudoers.workq" /etc/sudoers.d/workq
visudo -cf /etc/sudoers.d/workq >/dev/null

echo "== Claude Code managed settings"
install -d -m 755 /etc/claude-code
if [[ ! -f /etc/claude-code/managed-settings.json ]]; then
  install -m 644 "$SRC/etc/managed-settings.json" /etc/claude-code/managed-settings.json
else
  echo "   /etc/claude-code/managed-settings.json exists; compare with $SRC/etc/managed-settings.json"
fi

echo "== git key for the workq user"
if [[ ! -f /home/workq/.ssh/id_ed25519 && ! -f /home/workq/.ssh/id_rsa ]]; then
  sudo -u workq mkdir -p -m 700 /home/workq/.ssh
  # RSA because Azure DevOps accepts nothing else; GitHub accepts it too.
  sudo -u workq ssh-keygen -q -t rsa -b 4096 -N "" -C "workq@$(hostname)" -f /home/workq/.ssh/id_rsa
fi
for host in github.com ssh.dev.azure.com; do
  sudo -u workq sh -c "ssh-keyscan -t ed25519,rsa $host 2>/dev/null >> ~/.ssh/known_hosts" || true
done
sudo -u workq sh -c 'sort -u ~/.ssh/known_hosts -o ~/.ssh/known_hosts' || true

echo "== systemd units (timers not enabled yet)"
install -m 644 "$SRC"/systemd/workq@.service "$SRC"/systemd/workq-night.timer \
  "$SRC"/systemd/workq-digest.timer /etc/systemd/system/
systemctl daemon-reload 2>/dev/null || echo "   (systemd not running here; run: systemctl daemon-reload)"
if command -v restorecon >/dev/null; then   # SELinux: give the new files their default labels
  restorecon -R "$PREFIX" "$ETC" /etc/sudoers.d/workq /etc/claude-code /etc/systemd/system/workq@.service \
    /etc/systemd/system/workq-night.timer /etc/systemd/system/workq-digest.timer 2>/dev/null || true
fi

if ! sudo -u workq-agent -H bash -lc 'command -v claude' >/dev/null 2>&1 && ! command -v claude >/dev/null; then
  echo
  echo "!! Claude Code is not installed. Install it for the agent user:"
  echo "   sudo -u workq-agent -H bash -c 'curl -fsSL https://claude.ai/install.sh | bash'"
fi

echo
echo "Deploy key for the queue repo (add it with write access):"
cat /home/workq/.ssh/id_ed25519.pub 2>/dev/null || cat /home/workq/.ssh/id_rsa.pub
echo
echo "Next: follow 'Set up' in README.md from step 4."
