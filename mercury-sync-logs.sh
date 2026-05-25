#!/bin/bash
# mercury-sync-logs.sh — pull Claude Code + Codex logs from all known machines

set -uo pipefail
REMOTE_ROOT="$HOME/.mercury-cache/remote"
mkdir -p "$REMOTE_ROOT"

# Edit this list for YOUR machines.
# Format:  "alias|user@host_or_ssh_alias|optional_ssh_keyfile"
# Example:
#   "laptop-2|me@10.0.0.5|"
#   "gpu-server|admin@gpu.example.com|$HOME/.ssh/my-key"
#   "ssh-alias|my-alias|"      # uses ~/.ssh/config entry
HOSTS=(
  # "host-A|user@host|"
  # "host-B|user@host|$HOME/.ssh/keyfile"
)

sync_host() {
  local alias="$1" target="$2" key="$3"
  local dest="$REMOTE_ROOT/$alias"
  mkdir -p "$dest/claude" "$dest/codex/archived" "$dest/codex/sessions"

  local ssh_cmd="ssh -o ConnectTimeout=4 -o BatchMode=yes"
  [ -n "$key" ] && ssh_cmd="$ssh_cmd -i $key"

  echo "[$(date '+%H:%M:%S')] === $alias ($target) ==="

  if ! $ssh_cmd "$target" "true" 2>/dev/null; then
    echo "  unreachable, skip"
    return
  fi

  # Get remote $HOME so paths work on both macOS (/Users) and Linux (/home)
  local rhome
  rhome=$($ssh_cmd "$target" 'echo $HOME' 2>/dev/null)
  [ -z "$rhome" ] && { echo "  could not get HOME, skip"; return; }

  # Claude
  rsync -a --include='*.jsonl' --include='*/' --exclude='*' \
    -e "$ssh_cmd" \
    "$target:$rhome/.claude/projects/" "$dest/claude/" 2>/dev/null
  local n_cl=$(find "$dest/claude" -name '*.jsonl' 2>/dev/null | wc -l | xargs)
  echo "  claude:   $n_cl jsonl files"

  # Codex archived
  rsync -a --include='rollout-*.jsonl' --include='*/' --exclude='*' \
    -e "$ssh_cmd" \
    "$target:$rhome/.codex/archived_sessions/" "$dest/codex/archived/" 2>/dev/null
  # Codex live sessions
  rsync -a --include='rollout-*.jsonl' --include='*/' --exclude='*' \
    -e "$ssh_cmd" \
    "$target:$rhome/.codex/sessions/" "$dest/codex/sessions/" 2>/dev/null
  local n_cx=$(find "$dest/codex" -name 'rollout-*.jsonl' 2>/dev/null | wc -l | xargs)
  echo "  codex:    $n_cx rollouts"
}

main() {
  for h in "${HOSTS[@]}"; do
    IFS="|" read -r alias target key <<< "$h"
    sync_host "$alias" "$target" "$key"
  done
  echo "[$(date '+%H:%M:%S')] sync done. Total: $(find "$REMOTE_ROOT" -name '*.jsonl' | wc -l | xargs) jsonl files."
}

if [ "${1:-}" = "--loop" ]; then
  while true; do main; sleep 600; done
else
  main
fi
