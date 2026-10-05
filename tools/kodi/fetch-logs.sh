#!/usr/bin/env bash
# Pull a Kodi/Umbrella log bundle from the Chromecast over adb.
# Usage: fetch-logs.sh [adb-serial]   (prefers a connected Chromecast if omitted)
set -uo pipefail
umask 077

SERIAL="${1:-}"
if [ -z "$SERIAL" ]; then
  SERIAL=$(timeout 8 adb devices -l 2>/dev/null | awk '$2 == "device" && /model:Chromecast([[:space:]]|$)/ { print $1 }')
  if [[ "$SERIAL" == *$'\n'* ]]; then
    echo "multiple Chromecasts connected; specify an adb serial" >&2
    exit 1
  fi
  if [ -z "$SERIAL" ]; then
    while IFS= read -r candidate; do
      timeout 8 adb connect "$candidate" >/dev/null 2>&1 || continue
      model=$(timeout 8 adb -s "$candidate" shell getprop ro.product.model 2>/dev/null | tr -d '\r')
      if [ "$model" = Chromecast ]; then SERIAL="$candidate"; break; fi
    done < <(timeout 8 adb mdns services 2>/dev/null | awk '$2 == "_adb-tls-connect._tcp" { print $3 }' | sort -u)
  fi
fi
[ -z "$SERIAL" ] && { echo "no Chromecast found; pair Wireless debugging and supply its connection port" >&2; exit 1; }
export ANDROID_SERIAL="$SERIAL"
model=$(timeout 8 adb shell getprop ro.product.model 2>/dev/null | tr -d '\r')
[ "$model" = Chromecast ] || { echo "refusing to fetch logs from an unverified Chromecast: $SERIAL" >&2; exit 1; }

REMOTE=/sdcard/Android/data/org.xbmc.kodi/files/.kodi/temp
OUT="${KODI_LOG_ROOT:-$(dirname "$0")/../../logs}/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT" || exit 1

{
  date -u
  timeout 8 adb shell getprop ro.product.model
  timeout 8 adb shell getprop ro.build.version.release
  timeout 8 adb shell getprop ro.build.version.sdk
  echo "serial: $SERIAL"
  timeout 8 adb shell dumpsys package org.xbmc.kodi | awk '/versionCode|versionName/ { print; if (++n == 2) exit }'
  timeout 8 adb shell df -h /data /sdcard
  timeout 8 adb shell cat /proc/meminfo | awk '/^MemTotal:|^MemAvailable:/ { print }'
} > "$OUT/device-info.txt" 2>&1

for f in kodi.log kodi.old.log umbrella.log cocoscrapers.log; do
  if timeout 8 adb shell "[ -f $REMOTE/$f ]" 2>/dev/null; then
    timeout 30 adb pull "$REMOTE/$f" "$OUT/$f" >/dev/null 2>&1 && echo "pulled $f"
  fi
done

timeout 30 adb logcat -d -t 4000 > "$OUT/kodi-logcat.txt" 2>&1 && echo "pulled logcat"
timeout 30 adb logcat -d -b crash > "$OUT/android-crashes.txt" 2>&1 && echo "pulled native crash history"

# integrity: note sizes so a truncated pull is obvious
( cd "$OUT" && sha256sum ./* > SHA256SUMS 2>/dev/null )
echo "bundle: $OUT"
ls -la "$OUT"
