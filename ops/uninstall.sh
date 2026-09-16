#!/usr/bin/env bash
# Remove RowanJobs systemd units. The archive, backups and configuration are
# left alone: this script never deletes collected evidence.
set -euo pipefail
UNIT_DIR="$HOME/.config/systemd/user"
for unit in rowanjobs.timer rowanjobs-retry.timer; do
  systemctl --user disable --now "$unit" 2>/dev/null || true
done
for unit in rowanjobs.service rowanjobs.timer rowanjobs-retry.service rowanjobs-retry.timer; do
  rm -f "$UNIT_DIR/$unit"
done
systemctl --user daemon-reload
echo "units removed. The archive under the data root was NOT touched."
