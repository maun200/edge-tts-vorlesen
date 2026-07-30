#!/bin/bash
set -Eeuo pipefail
export LC_NUMERIC=C
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export PULSE_SERVER="unix:${XDG_RUNTIME_DIR}/pulse/native"
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

LOG="/tmp/edge-tts-wrapper.log"
DATA=$(cat)
VOICE="${1:-de-DE-KillianNeural}"
RATE_IN="${2:-0}"
# Robuste Integer-Konvertierung ohne printf auf Float
RATE_INT="${RATE_IN%.*}"
[[ -z "$RATE_INT" || "$RATE_INT" == "-" ]] && RATE_INT=0
if [[ "$RATE_INT" -ge 0 ]]; then
  RATE="+${RATE_INT}%"
else
  RATE="${RATE_INT}%"
fi
DATA="${DATA//$'\n'/ }"
{
  echo "=== $(date -Is) ==="
  echo "VOICE=$VOICE RATE=$RATE LEN=${#DATA}"
  edge-playback --text="$DATA" --voice="$VOICE" --rate="$RATE"
  echo "=== done ==="
} >>"$LOG" 2>&1
