#!/usr/bin/env bash
# Convert a screen recording to demo/collections-agent-demo.mp4 and sanity-check it.
#
#   demo/convert-to-mp4.sh                      # finds the newest recording automatically
#   demo/convert-to-mp4.sh path/to/input.webm   # or name one explicitly
#
# Needs ffmpeg:  sudo apt install ffmpeg

set -euo pipefail
cd "$(dirname "$0")/.."
OUT="demo/collections-agent-demo.mp4"

command -v ffmpeg >/dev/null || { echo "ffmpeg not installed: sudo apt install ffmpeg"; exit 1; }

if [ $# -ge 1 ]; then
  IN="$1"
else
  # Newest recording from either OBS's default dir or GNOME's.
  IN=$(ls -t ~/Videos/*.mkv ~/Videos/*.mp4 ~/Videos/*.webm \
          ~/Videos/Screencasts/*.webm ~/Videos/Screencasts/*.mp4 2>/dev/null | head -1 || true)
  [ -n "$IN" ] || { echo "No recording found in ~/Videos. Pass the path explicitly."; exit 1; }
fi

echo "input : $IN"
ffmpeg -hide_banner -loglevel error -y -i "$IN" \
       -c:v libx264 -preset medium -crf 23 -pix_fmt yuv420p \
       -c:a aac -b:a 128k -movflags +faststart "$OUT"

echo "output: $OUT"
echo
# Report what actually landed, so nothing is taken on trust.
ffprobe -v error -show_entries format=duration,size \
        -show_entries stream=codec_type,codec_name,width,height \
        -of default=noprint_wrappers=1 "$OUT"
DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT" | cut -d. -f1)
echo
echo "duration: ${DUR}s"
[ "${DUR:-0}" -ge 120 ] || echo "WARNING: under 2 minutes - is that the whole demo?"
ffprobe -v error -show_streams "$OUT" | grep -q 'codec_type=audio' \
  && echo "audio: present" || echo "audio: NONE (silent recording)"
echo
echo "GitHub rejects files over 100MB. Size check:"
du -h "$OUT"
