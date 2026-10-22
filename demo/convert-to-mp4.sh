#!/usr/bin/env bash
# Convert a screen recording to demo/collections-agent-demo.mp4 and verify the result.
#
#   demo/convert-to-mp4.sh                    # auto-pick the newest *recent* recording
#   demo/convert-to-mp4.sh path/to/input.mkv  # or name one explicitly
#   demo/convert-to-mp4.sh --force            # accept an old file (see staleness guard)
#
# Needs ffmpeg:  sudo apt install ffmpeg

set -euo pipefail
cd "$(dirname "$0")/.."
OUT="demo/collections-agent-demo.mp4"
MAX_AGE_MIN="${MAX_AGE_MIN:-180}"   # refuse anything older than this unless --force

command -v ffmpeg  >/dev/null || { echo "ffmpeg not installed:  sudo apt install ffmpeg"; exit 1; }
command -v ffprobe >/dev/null || { echo "ffprobe not installed: sudo apt install ffmpeg"; exit 1; }

FORCE=0
IN=""
for a in "$@"; do
  case "$a" in
    --force) FORCE=1 ;;
    *) IN="$a" ;;
  esac
done

if [ -z "$IN" ]; then
  # demo/ first - that is where OBS is configured to write. ~/Videos is only a
  # fallback, and it commonly holds unrelated older screencasts.
  IN=$(ls -t demo/*.mkv demo/*.webm demo/*.mp4 \
          ~/Videos/*.mkv ~/Videos/*.webm ~/Videos/*.mp4 \
          ~/Videos/Screencasts/*.mkv ~/Videos/Screencasts/*.webm ~/Videos/Screencasts/*.mp4 \
          2>/dev/null | grep -v "^$OUT\$" | head -1 || true)
  [ -n "$IN" ] || { echo "No recording found in demo/ or ~/Videos. Record one, or pass a path."; exit 1; }
fi

[ -f "$IN" ] || { echo "Not a file: $IN"; exit 1; }
[ -s "$IN" ] || { echo "Input is empty: $IN"; exit 1; }

AGE_MIN=$(( ( $(date +%s) - $(stat -c %Y "$IN") ) / 60 ))
echo "input : $IN"
echo "        modified $(stat -c %y "$IN" | cut -d. -f1)  (${AGE_MIN} min ago)  $(du -h "$IN" | cut -f1)"

# Staleness guard. ~/Videos/Screencasts often contains unrelated recordings from
# months ago; converting one of those into the submission would be worse than
# having no video at all.
if [ "$AGE_MIN" -gt "$MAX_AGE_MIN" ] && [ "$FORCE" -eq 0 ]; then
  echo
  echo "REFUSING: that file is ${AGE_MIN} minutes old (limit ${MAX_AGE_MIN})."
  echo "It is probably not the recording you just made. Candidates:"
  ls -lt demo/*.mkv demo/*.webm ~/Videos/*.mkv ~/Videos/*.webm \
         ~/Videos/Screencasts/*.webm ~/Videos/Screencasts/*.mkv 2>/dev/null \
    | head -8 | awk '{printf "  %s %s %s %s\n", $6, $7, $8, $9}'
  echo
  echo "Pass the right path explicitly, or re-run with --force if this really is it."
  exit 2
fi

echo
ffmpeg -hide_banner -loglevel error -y -i "$IN" \
       -c:v libx264 -preset medium -crf 23 -pix_fmt yuv420p \
       -c:a aac -b:a 128k -movflags +faststart "$OUT"

# ---- verification -----------------------------------------------------------
echo "output: $OUT"
echo
[ -s "$OUT" ] || { echo "FAIL: output is empty"; exit 1; }

DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT" | cut -d. -f1)
VID=$(ffprobe -v error -select_streams v -show_entries stream=codec_name,width,height -of csv=p=0 "$OUT" | head -1)
AUD=$(ffprobe -v error -select_streams a -show_entries stream=codec_name,channels -of csv=p=0 "$OUT" | head -1)
SIZE=$(du -h "$OUT" | cut -f1)
SIZE_MB=$(( $(stat -c %s "$OUT") / 1048576 ))

echo "  duration : ${DUR}s ($((DUR/60))m $((DUR%60))s)"
echo "  video    : ${VID:-NONE}"
echo "  audio    : ${AUD:-NONE - silent recording}"
echo "  size     : $SIZE"

echo
echo "  decoding the whole file to check for corruption..."
ERRS=$(ffmpeg -v error -i "$OUT" -f null - 2>&1 | head -5)
if [ -n "$ERRS" ]; then
  echo "  DECODE ERRORS:"; printf '%s\n' "$ERRS" | sed 's/^/    /'
else
  echo "  decodes cleanly, no errors"
fi

echo
STATUS=0
[ "${DUR:-0}" -ge 120 ] || { echo "  WARN: under 2 minutes - is that the whole demo?"; STATUS=1; }
[ -n "$AUD" ]           || { echo "  WARN: no audio track - the demo will be silent"; STATUS=1; }
[ "$SIZE_MB" -lt 100 ]  || { echo "  WARN: ${SIZE_MB}MB exceeds GitHub's 100MB limit - host it externally"; STATUS=1; }
[ "$STATUS" -eq 0 ] && echo "  All checks passed."
exit 0
