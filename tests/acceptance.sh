#!/usr/bin/env bash
# Fresh-machine acceptance run: a new user follows README.md from download to a scheduled run.
# Run as root in a throwaway Ubuntu VM: sudo bash tests/acceptance.sh [candidate coldvault.py]
# It creates a user and moves the clock to just before the hour so cron fires.
set -euo pipefail

kit=$(cd "$(dirname "$0")/.." && pwd)
readme=$kit/README.md
candidate=${1:+$(realpath "$1")}
user=coldvault-acceptance
limit=600

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 2; }
systemd-detect-virt --vm -q || { echo "refusing to run outside a throwaway VM" >&2; exit 2; }
id "$user" >/dev/null 2>&1 && { echo "user $user already exists; use a fresh VM" >&2; exit 2; }

start=$(cut -d' ' -f1 /proc/uptime)
elapsed() { awk -v s="$start" '{printf "%d", $1 - s}' /proc/uptime; }

step() {
  local line left
  for line in "$@"; do
    grep -qF -- "$line" "$readme" || { echo "FAIL: not in README: $line" >&2; exit 1; }
  done
  left=$((limit - $(elapsed)))
  [ "$left" -gt 0 ] || { echo "FAIL: over ${limit}s before: $1" >&2; exit 1; }
  printf '+ %s\n' "$@"
  timeout "$left" su - "$user" -c "$(printf '%s\n' "set -e" "$@")"
}

useradd -m -s /bin/bash "$user"
home=$(getent passwd "$user" | cut -d: -f6)
install -d -o "$user" -g "$user" "$home/.claude" "$home/.claude/projects"
install -o "$user" -g "$user" -m 600 "$kit/examples/transcripts/claude/demo-session.jsonl" "$home/.claude/projects/"

if [ -n "$candidate" ]; then
  grep -qF "curl -O https://raw.githubusercontent.com/TayHCode/coldvault/main/coldvault.py" "$readme"
  echo "+ (candidate) install $candidate as ~/coldvault/coldvault.py instead of the README download"
  install -d -o "$user" -g "$user" "$home/coldvault"
  install -o "$user" -g "$user" -m 644 "$candidate" "$home/coldvault/coldvault.py"
else
  step "mkdir -p ~/coldvault && cd ~/coldvault" \
       "curl -O https://raw.githubusercontent.com/TayHCode/coldvault/main/coldvault.py"
fi

printf '%s\n' "$home/ColdvaultVault" '' '' '' '' '' | step "cd ~/coldvault" "python3 coldvault.py setup"
step "python3 ~/coldvault/coldvault.py schedule --vault ~/ColdvaultVault"

runs=$home/ColdvaultVault/.coldvault/runs.jsonl
before=$(wc -l < "$runs")
trap 'timedatectl set-ntp true' EXIT
timedatectl set-ntp false
date -s "$(date -d "$(date -d '+1 hour' '+%F %H:00:00') 20 seconds ago")" >/dev/null
echo "+ (clock) moved to $(date '+%F %T'); waiting for the scheduled run"
until [ "$(wc -l < "$runs")" -gt "$before" ] && [ "$(tail -n1 "$runs")" = "$(cat "$home/ColdvaultVault/.coldvault/last-run.json" 2>/dev/null)" ]; do
  [ "$(elapsed)" -lt "$limit" ] || { echo "FAIL: no scheduled run after $(elapsed)s" >&2; exit 1; }
  sleep 5
done

step "python3 ~/coldvault/coldvault.py status --vault ~/ColdvaultVault"

seconds=$(elapsed)
echo "PASS: README run finished in ${seconds}s (limit ${limit}s)"
[ "$seconds" -lt "$limit" ]
