#!/bin/bash
# kernel_counters.sh -s SECONDS | -- COMMAND [ARGS...] | -a | -l
#
# Shows which of the driver's kernel counters (debug.nvaccel*, debug.nvrm*) moved during one interaction, so a report can say
# "debug.nvaccel_iop_flip_refused went up by 300 while I dragged a window" instead of pasting a few absolute values.
#   -s N        sample, sleep N seconds, sample again; print the counters that changed, with their rate
#   -- COMMAND  sample, run COMMAND, sample again; the exit status is COMMAND's
#   -a          print every counter once, for pasting into an issue
#   -l          list the counter names
# Read-only: it only runs sysctl (no sudo). Names are discovered, not listed here, so a counter added to a kext shows up by
# itself. Read-write tuning knobs are listed too; they simply never change. Exit status 3: the kexts are not loaded.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
# Present in a checkout, normally absent on a user's machine; then names are printed without a meaning.
SRC=$HERE/../kexts/NVRM/accel/nvrm-accel.cpp

usage() { echo "usage: ${0##*/} -s SECONDS | -- COMMAND [ARGS...] | -a | -l" >&2; exit 2; }

mode="" secs=""
while [ $# -gt 0 ]; do
  case $1 in
    -a|-l) mode=$1 ;;
    -s) [ $# -ge 2 ] || usage; secs=$2; shift ;;
    --) shift; break ;;
    *) usage ;;
  esac
  shift
done
if [ -n "$secs" ]; then
  [ -z "$mode" ] && [ $# -eq 0 ] || usage
  case $secs in ''|*[!0-9]*) usage ;; esac; mode=-s
elif [ $# -gt 0 ]; then
  [ -z "$mode" ] || usage; mode=-c
fi
[ -n "$mode" ] || usage

T=$(mktemp -d /tmp/kernel-counters.XXXXXX) || exit 2
trap 'rm -rf "$T"' EXIT

# "name value" per line, from the numeric sysctls only (the string ones are dumps, not counters).
sample() {
  sysctl -a 2>/dev/null | sed -n -E 's/^(debug\.(nvaccel|nvrm)[A-Za-z0-9_]*): (-?[0-9]+)$/\1 \3/p' | LC_ALL=C sort
}

# The kext can be absent at the start, or unloaded while the command ran.
need_counters() {
  [ -s "$1" ] && return
  echo "the NullMoth kernel extensions are not loaded (no debug.nvaccel* / debug.nvrmfb* sysctls)" >&2
  exit 3
}

header() {
  echo "macOS   $(sw_vers -productVersion) ($(sw_vers -buildVersion))   $(uname -m)"
  command -v kextstat >/dev/null 2>&1 || return 0
  kexts=$(kextstat 2>/dev/null | sed -n 's/.*\(com\.nullmoth\.[A-Za-z0-9_.]*\) (\([^)]*\)).*/\1 \2/p' |
    awk '{ printf "%s%s %s", s, $1, $2; s = ", " }')
  [ -z "$kexts" ] || echo "Kexts   $kexts"
}

# report ALL ELAPSED BEFORE AFTER DESCS: ALL=1 prints every counter, else only those that differ. Counters are grouped by the
# first two words of the name (nvaccel_iop, nvrmfb_vram, ...); the input is sorted, so a group is contiguous.
report() {
  awk -v all="$1" -v el="$2" '
    FNR == 1 { f++ }
    f == 1 { b[$1] = $2; next }
    f == 2 { name[++n] = $1; a[$1] = $2; next }
    { d[$1] = substr($0, length($1) + 2) }
    END {
      for (i = 1; i <= n; i++) {
        k = name[i]; kn = (k in b)
        if (!all && kn && b[k] "" == a[k] "") { same++; continue }
        split(k, p, "_"); g = p[1] "_" p[2]; sub(/^debug\./, "", g)
        if (!head++) {
          if (all) printf "  %-36s %14s\n", "counter", "value"
          else printf "  %-36s %14s %14s %14s %10s\n", "counter", "before", "after", "delta", "per second"
        }
        if (g != last) { printf "%s%s\n", (last == "" ? "" : "\n"), g; last = g }
        note = (k in d) ? "  # " d[k] : ""
        if (all) { printf "  %-36s %14s%s\n", k, a[k], note; continue }
        delta = a[k] - b[k]
        printf "  %-36s %14s %14s %14s %10s%s\n", k, kn ? b[k] : "-", a[k], kn ? sprintf("%+.0f", delta) : "-",
          (kn && el > 0) ? sprintf("%.1f", delta / el) : "-", note
      }
      if (!all) printf "%sunchanged: %d counters\n", head ? "\n" : "", same
    }' "$3" "$4" "$5"
}

: > "$T/desc"
# NVACCEL_CNT(name, variable, "meaning") -> "debug.name meaning"
[ -f "$SRC" ] && sed -n 's/^NVACCEL_CNT(\([a-z0-9_]*\), *[^,]*, *"\(.*\)");.*/debug.\1 \2/p' "$SRC" > "$T/desc"

sample > "$T/before"
need_counters "$T/before"
case $mode in
  -l) cut -d' ' -f1 "$T/before"; exit 0 ;;
  -a) header; echo; report 1 0 "$T/before" "$T/before" "$T/desc"; exit 0 ;;
esac

header
echo
rc=0
if [ "$mode" = -s ]; then
  sleep "$secs"; elapsed=$secs      # the requested time: whole seconds from date would be coarser for the rate
else
  start=$(date +%s)
  "$@"; rc=$?
  elapsed=$(( $(date +%s) - start ))
fi
sample > "$T/after"
need_counters "$T/after"
echo "elapsed: $elapsed s"
report 0 "$elapsed" "$T/before" "$T/after" "$T/desc"
exit "$rc"
