#!/usr/bin/env bash
# Post-reboot check: is the display stack healthy, and does playback work?
# Usage: ./verify.sh
set -uo pipefail
cd "$(dirname "$0")"

TEST_STREAM='https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8'
fail=0

echo "=== 1. kernel DRM state ==="
if sudo -n dmesg 2>/dev/null | grep -qiE 'Unable to handle kernel|Internal error: Oops'; then
    echo "FAIL: a kernel oops is in this boot's log - the display may wedge again"
    sudo -n dmesg | grep -iE 'Unable to handle kernel|Internal error: Oops' | tail -3
    fail=1
else
    echo "OK: no kernel oops this boot"
fi

echo
echo "=== 2. X server responds (this hangs when the stack is wedged) ==="
if timeout 10 env DISPLAY=:0 xrandr >/dev/null 2>&1; then
    echo "OK: X is responsive"
else
    echo "FAIL: xrandr hung or errored - display stack is not healthy"
    fail=1
fi

echo
echo "=== 3. detection ==="
python3 stream.py --list-displays || fail=1

echo
echo "=== 4. real playback, 6 seconds on the TV ==="
if timeout 60 python3 stream.py --retries 0 \
        --mpv-arg=--length=6 --mpv-arg=--msg-level=all=info \
        "$TEST_STREAM" 2>&1 | grep -E 'VO:|AO:|Exiting'; then
    echo "OK: video and audio opened"
else
    echo "FAIL: no playback - see output above"
    fail=1
fi

echo
if [ "$fail" -eq 0 ]; then
    echo "ALL CHECKS PASSED"
else
    echo "SOME CHECKS FAILED (exit $fail)"
fi
exit "$fail"
