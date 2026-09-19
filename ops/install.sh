#!/usr/bin/env bash
# Idempotent RowanJobs deployment.
#
#   ops/install.sh [--data-root DIR] [--config FILE] [--no-enable]
#
# Installs the locked project environment, writes systemd *user* units with
# absolute resolved paths, enables the daily timer, and records a runtime
# deployment manifest outside Git. Safe to re-run: every step converges.
#
# It deliberately does NOT: open a listening port, touch any other project's
# units, use sudo for anything beyond an optional `loginctl enable-linger`, or
# modify ControlPanel.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_ROOT="${ROWANJOBS_DATA_ROOT:-$HOME/.local/share/rowanjobs}"
CONFIG_DIR="$HOME/.config/rowanjobs"
CONFIG_FILE="${ROWANJOBS_CONFIG:-$CONFIG_DIR/config.toml}"
UNIT_DIR="$HOME/.config/systemd/user"
VENV="$REPO_ROOT/.venv"
ENABLE=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --data-root) DATA_ROOT="$2"; shift 2 ;;
    --config)    CONFIG_FILE="$2"; shift 2 ;;
    --no-enable) ENABLE=0; shift ;;
    -h|--help)   sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

say() { printf '==> %s\n' "$*"; }

say "repository      : $REPO_ROOT"
say "data root       : $DATA_ROOT"
say "configuration   : $CONFIG_FILE"
say "systemd units   : $UNIT_DIR"

# ---------------------------------------------------------------- environment
if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required (https://docs.astral.sh/uv/)" >&2
  exit 1
fi

# Production environment: locked, no dev tooling, but WITH the browser extra so
# the access-control fallback is available. Re-run `uv sync --all-extras` to get
# the development toolchain (ruff, mypy, pytest) back into the same venv.
say "syncing the locked project environment"
( cd "$REPO_ROOT" && uv sync --frozen --no-dev --extra browser ) \
  || ( cd "$REPO_ROOT" && uv sync --no-dev --extra browser )
test -x "$VENV/bin/rowanjobs" || { echo "rowanjobs entry point missing in $VENV" >&2; exit 1; }

# ------------------------------------------------------------------- storage
install -d -m 700 "$DATA_ROOT" "$DATA_ROOT/backups" "$DATA_ROOT/logs" \
                  "$DATA_ROOT/runtime" "$DATA_ROOT/source-audit" "$DATA_ROOT/exports"
install -d -m 700 "$CONFIG_DIR"

if [[ ! -f "$CONFIG_FILE" ]]; then
  say "writing a default configuration (existing files are never overwritten)"
  install -m 600 "$REPO_ROOT/config.example.toml" "$CONFIG_FILE"
else
  say "configuration already present; leaving it untouched"
fi

say "applying schema migrations"
"$VENV/bin/rowanjobs" --config "$CONFIG_FILE" --data-root "$DATA_ROOT" migrate

# --------------------------------------------------------------------- units
install -d -m 755 "$UNIT_DIR"
for unit in rowanjobs.service rowanjobs.timer rowanjobs-retry.service rowanjobs-retry.timer \
            rowanjobs-notify.service rowanjobs-notify.timer 'rowanjobs-failure@.service'; do
  sed -e "s|__VENV__|$VENV|g" \
      -e "s|__APP_DIR__|$REPO_ROOT|g" \
      -e "s|__DATA_ROOT__|$DATA_ROOT|g" \
      -e "s|__CONFIG__|$CONFIG_FILE|g" \
      "$REPO_ROOT/ops/systemd/$unit" > "$UNIT_DIR/$unit.tmp"
  if [[ -f "$UNIT_DIR/$unit" ]] && cmp -s "$UNIT_DIR/$unit.tmp" "$UNIT_DIR/$unit"; then
    rm -f "$UNIT_DIR/$unit.tmp"
    say "unit unchanged: $unit"
  else
    mv "$UNIT_DIR/$unit.tmp" "$UNIT_DIR/$unit"
    say "installed unit: $unit"
  fi
done

systemctl --user daemon-reload

say "validating the calendar expression"
systemd-analyze calendar "*-*-* 06:15:00 America/New_York"

if [[ "$ENABLE" == "1" ]]; then
  systemctl --user enable --now rowanjobs.timer
  systemctl --user enable --now rowanjobs-retry.timer
  # Mail-only retry. An implemented notify command with no scheduled caller is
  # not a retry policy.
  systemctl --user enable --now rowanjobs-notify.timer
  say "timers enabled"
fi

# ------------------------------------------------------------------ lingering
if [[ -e "/var/lib/systemd/linger/$USER" ]]; then
  say "user lingering already enabled: unattended execution will work"
else
  say "user lingering is NOT enabled; user units will not run without a login."
  say "enable it with:  sudo loginctl enable-linger $USER"
fi

"$VENV/bin/rowanjobs" --config "$CONFIG_FILE" --data-root "$DATA_ROOT" \
    record-deployment --note "ops/install.sh"

say "next activation:"
systemctl --user list-timers rowanjobs.timer rowanjobs-retry.timer \
    rowanjobs-notify.timer --no-pager || true

say "done. Check with:  rowanjobs doctor"
