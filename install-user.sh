#!/bin/bash
# Installs workq for the current user. Needs no root access and changes nothing
# outside your home folder. Built for RHEL 9 and its rebuilds; works on any Linux
# with Python 3.9+ and git.
#
#   ./install-user.sh              install or upgrade (never overwrites your config or keys)
#   ./install-user.sh --uninstall  remove the program and its schedule; keeps config and state
set -euo pipefail
umask 077

SRC="$(cd "$(dirname "$0")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/workq"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/workq"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/workq"
BIN="$HOME/.local/bin"
KEY="$HOME/.ssh/workq_key"

if [[ $EUID -eq 0 ]]; then
  echo "Run this as the user who will own the queue, not as root." >&2
  echo "(For a root install with a separate agent user, use install.sh.)" >&2
  exit 1
fi

if [[ "${1:-}" == "--uninstall" ]]; then
  if [[ -x "$DATA/bin/workq" && -f "$CONF/config.yaml" ]]; then
    "$DATA/bin/workq" --config "$CONF/config.yaml" schedule off || true
  fi
  rm -rf "$DATA"
  [[ -L "$BIN/workq" ]] && rm -f "$BIN/workq"
  echo "Removed the program. Kept: $CONF (config), $STATE (state), $KEY (git key)."
  exit 0
fi

echo "== checking what this machine has"
missing=()
if ! command -v python3 >/dev/null || ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then
  missing+=("python3 (3.9 or later)")
fi
command -v git >/dev/null || missing+=("git")
if (( ${#missing[@]} )); then
  echo "Missing: ${missing[*]}" >&2
  echo "These need an admin. On RHEL 9: sudo dnf install -y python3 git openssh-clients" >&2
  exit 1
fi
echo "   $(python3 --version), $(git --version)"

echo "== program files -> $DATA"
mkdir -p "$DATA" "$BIN"
rm -rf "$DATA/bin" "$DATA/lib" "$DATA/template" "$DATA/etc"
cp -r "$SRC/bin" "$SRC/lib" "$SRC/template" "$SRC/etc" "$DATA/"
find "$DATA" -name '__pycache__' -prune -exec rm -rf {} +
chmod 700 "$DATA"
chmod 755 "$DATA/bin/workq"
ln -sfn "$DATA/bin/workq" "$BIN/workq"

echo "== config -> $CONF"
mkdir -p "$CONF" "$STATE"
chmod 700 "$CONF" "$STATE"
CLAUDE="$(command -v claude || true)"
[[ -z "$CLAUDE" && -x "$BIN/claude" ]] && CLAUDE="$BIN/claude"
if [[ ! -f "$CONF/config.yaml" ]]; then
  sed -e "s|__STATE__|$STATE|g" -e "s|__CONF__|$CONF|g" -e "s|__KEY__|$KEY|g" \
      -e "s|__CLAUDE__|${CLAUDE:-claude}|g" "$SRC/etc/config.user.example.yaml" > "$CONF/config.yaml"
  echo "   wrote config.yaml: edit it before first use"
else
  echo "   kept your config.yaml (new options are listed in $DATA/etc/config.user.example.yaml)"
fi
if [[ ! -f "$CONF/claude.env" ]]; then
  cp "$SRC/etc/claude.env.example" "$CONF/claude.env"
  echo "   wrote claude.env: add the Claude token"
fi
[[ -f "$CONF/claude-settings.json" ]] || cp "$SRC/etc/claude-settings.user.json" "$CONF/claude-settings.json"
chmod 600 "$CONF/config.yaml" "$CONF/claude.env" "$CONF/claude-settings.json"

echo "== git key for the queue repo"
if [[ -f "$KEY" ]]; then
  echo "   kept $KEY"
elif command -v ssh-keygen >/dev/null; then
  mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
  # RSA because Azure DevOps accepts nothing else; GitHub accepts it too.
  ssh-keygen -q -t rsa -b 4096 -N "" -C "workq@$(hostname -s 2>/dev/null || echo host)" -f "$KEY"
  echo "   made $KEY"
else
  echo "   ssh-keygen not found: use HTTPS with a token instead (git.credential_file in config.yaml)"
fi

echo
echo "Installed workq $("$DATA/bin/workq" --version)."
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "Note: $BIN is not on your PATH. Add it (echo 'export PATH=\$HOME/.local/bin:\$PATH' >> ~/.bashrc) or call $BIN/workq." ;;
esac
if [[ -z "$CLAUDE" ]]; then
  echo
  echo "Claude Code is not installed yet. It installs into your home, no root needed:"
  echo "   curl -fsSL https://claude.ai/install.sh | bash"
  echo "then set claude.bin in config.yaml to $BIN/claude if it isn't found."
fi
if [[ -f "$KEY.pub" ]]; then
  echo
  echo "Public key to add to the queue repo, with write access"
  echo "(Azure DevOps: User settings > SSH public keys. GitHub: repo Settings > Deploy keys):"
  cat "$KEY.pub"
fi
cat <<EOF

Next:
  1. Add the key above to the repo (or set up HTTPS with a token).
  2. Put the Claude token in $CONF/claude.env
  3. Edit $CONF/config.yaml (repo_url, web_url, caps; email is optional)
  4. workq doctor --live      checks everything, including one tiny real call to Claude
  5. workq init               fills an empty queue repo (skip if it is already set up)
  6. workq schedule on        turns on the nightly shift and the morning digest
EOF
