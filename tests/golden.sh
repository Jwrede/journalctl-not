#!/usr/bin/env bash
# Compare journalctl-not (without exclusions) byte for byte with journalctl on the
# local journal. Writes a few tricky test entries first. Needs read access to the
# journal: run as root, or set SUDO="sudo -n".
set -euo pipefail
cd "$(dirname "$0")/.."
SUDO=${SUDO:-}
PY=${PY:-python3}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

tag="jnot-golden-$$"
since=@$(date +%s)
"$PY" tests/write_entries.py "$tag"
sleep 2
until=@$(( $(date +%s) + 1 ))
sleep 2

fail=0
compare() {
  local label=$1; shift
  $SUDO env SYSTEMD_URLIFY=0 "${ENVS[@]}" journalctl --no-pager "$@" > "$tmp/want" 2>/dev/null || true
  $SUDO env SYSTEMD_URLIFY=0 PYTHONPATH=src "${ENVS[@]}" "$PY" -m journalctl_not.cli --no-pager "$@" > "$tmp/got" 2>"$tmp/err" || true
  if cmp -s "$tmp/want" "$tmp/got"; then
    echo "ok   $label ($(wc -l < "$tmp/want") lines)"
  else
    echo "FAIL $label"; diff "$tmp/want" "$tmp/got" | head -20 | cut -c1-200 | cat -A || true; cat "$tmp/err"; fail=1
  fi
}

# An exclusion against the journalctl filter that means the same.
excluded_vs() {
  local label=$1 want=$2 got=$3
  $SUDO env TZ=UTC journalctl --no-pager --since "$since" --until "$until" $want > "$tmp/want" 2>/dev/null || true
  $SUDO env TZ=UTC PYTHONPATH=src "$PY" -m journalctl_not.cli --no-pager --since "$since" --until "$until" $got > "$tmp/got" 2>"$tmp/err" || true
  if cmp -s "$tmp/want" "$tmp/got"; then
    echo "ok   $label ($(wc -l < "$tmp/want") lines)"
  else
    echo "FAIL $label"; diff "$tmp/want" "$tmp/got" | head -20 | cut -c1-200 | cat -A || true; cat "$tmp/err"; fail=1
  fi
}
excluded_vs "PRIORITY!=7 is -p info" "-t $tag -p info" "-t $tag PRIORITY!=7"
excluded_vs "-n counts kept entries" "-t $tag -p info -n 4" "-t $tag PRIORITY!=7 -n 4"
excluded_vs "--exclude-grep" "-t $tag -p info" "-t $tag --exclude-grep=^(noise|priority.7)"

ENVS=(TZ=UTC)
for mode in short short-precise short-iso short-iso-precise short-full short-unix short-monotonic cat; do
  compare "test entries -o $mode" --since "$since" --until "$until" -o "$mode"
done
compare "test entries -a" --since "$since" --until "$until" -a
compare "test entries -r --no-hostname" --since "$since" --until "$until" -r --no-hostname
compare "test entries -n 3" --since "$since" --until "$until" -t "$tag" -n 3
ENVS=(TZ=UTC SYSTEMD_COLORS=true)
compare "test entries, colors" --since "$since" --until "$until"
compare "test entries, colors, cat" --since "$since" --until "$until" -o cat
ENVS=(TZ=Europe/Berlin)
compare "test entries, Europe/Berlin" --since "$since" --until "$until" -o short-full
ENVS=(TZ=UTC)
# A time window, not -n: journalctl 255 with -n N --until T and fewer than N
# entries also prints entries after T.
window=(--since "@$(( ${since#@} - 6 * 3600 ))" --until "$since")
compare "whole journal, last 6 hours" "${window[@]}"
compare "whole journal, -p warning" "${window[@]}" -p warning
compare "kernel" "${window[@]}" -k
exit $fail
