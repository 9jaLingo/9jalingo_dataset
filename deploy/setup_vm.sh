#!/usr/bin/env bash
# One-time setup for the 9jaLingo review server on this VM.
#
# Run this AS THE USER that will own the app and the GitHub Actions runner
# (not root), from anywhere -- it locates the repo relative to this script.
# It is safe to re-run: every step is idempotent.
#
#   cd ~/9jalingo_dataset   # wherever you cloned github.com/9jaLingo/9jalingo_dataset
#   bash deploy/setup_vm.sh
#
# What it does:
#   1. Creates the venv and installs review_server's dependencies
#   2. Scaffolds /etc/9jalingo-review/env for secrets (HF_TOKEN etc) if
#      missing -- YOU must edit it before the service will start
#   3. Installs and enables a systemd service that runs uvicorn
#   4. Grants passwordless `systemctl restart` on that one service, so the
#      GitHub Actions deploy workflow can restart it without a sudo prompt
#
# What it does NOT do (see deploy/README.md for these, they're inherently
# manual/interactive steps):
#   - Register the GitHub Actions self-hosted runner (needs a short-lived
#     token from the GitHub UI)
#   - Open port 8787 in the Azure NSG / set up a reverse proxy

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_DIR="$REPO_ROOT/review_server"
SERVICE_NAME="9jalingo-review"
RUN_USER="$(whoami)"
ENV_FILE="/etc/9jalingo-review/env"

if [ ! -f "$APP_DIR/main.py" ]; then
  echo "error: expected $APP_DIR/main.py -- run this from inside the cloned repo" >&2
  exit 1
fi

echo "==> [1/4] Python venv + dependencies in $APP_DIR/.venv"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "==> [2/4] Secrets file at $ENV_FILE"
if [ ! -f "$ENV_FILE" ]; then
  sudo mkdir -p "$(dirname "$ENV_FILE")"
  sudo tee "$ENV_FILE" > /dev/null <<'EOF'
HF_TOKEN=
REVIEW_TOKEN=
TARGET_REPO_TEMPLATE=voicedata/9jalingo-reviewed-{language}
EOF
  sudo chmod 600 "$ENV_FILE"
  sudo chown "$RUN_USER":"$RUN_USER" "$ENV_FILE"
  echo "    created $ENV_FILE -- EDIT IT NOW and fill in HF_TOKEN (and REVIEW_TOKEN if you use one)"
else
  echo "    already exists, leaving it alone"
fi

echo "==> [3/4] systemd service ($SERVICE_NAME)"
sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" > /dev/null <<EOF
[Unit]
Description=9jaLingo dataset review server
After=network.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${APP_DIR}/.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8787
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

echo "==> [4/4] Passwordless restart permission for the deploy workflow"
SUDOERS_FILE="/etc/sudoers.d/${SERVICE_NAME}"
SUDOERS_LINE="${RUN_USER} ALL=(ALL) NOPASSWD: /bin/systemctl restart ${SERVICE_NAME}"
if [ ! -f "$SUDOERS_FILE" ] || ! sudo grep -qF "$SUDOERS_LINE" "$SUDOERS_FILE"; then
  echo "$SUDOERS_LINE" | sudo tee "$SUDOERS_FILE" > /dev/null
  sudo chmod 440 "$SUDOERS_FILE"
  sudo visudo -cf "$SUDOERS_FILE"
fi

echo
if [ -s "$ENV_FILE" ] && grep -q '^HF_TOKEN=$' "$ENV_FILE"; then
  echo "!! $ENV_FILE still has an empty HF_TOKEN -- fill it in, then run:"
  echo "     sudo systemctl start $SERVICE_NAME"
else
  sudo systemctl restart "$SERVICE_NAME"
  echo "Service started. Check it with:"
  echo "     sudo systemctl status $SERVICE_NAME"
  echo "     curl http://127.0.0.1:8787/"
fi
echo
echo "Next: install the GitHub Actions self-hosted runner -- see deploy/README.md"
