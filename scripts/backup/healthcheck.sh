#!/bin/sh
set -eu
test -f /backups/.last-success
test ! -f /backups/.last-failure
age=$(( $(date +%s) - $(stat -c %Y /backups/.last-success) ))
test "$age" -le "$(( ${BACKUP_INTERVAL_SECONDS:-86400} * 2 + 900 ))"
