#!/usr/bin/env bash
# Post-reboot check: is the display stack healthy, and is the kiosk ready?
# Usage: ./verify.sh [stream-url]
#        With a URL, it also plays it on the TV for 45 seconds as a real test.
set -uo pipefail
cd "$(dirname "$0")"

URL="${1:-}"
PROFILE="${STREAM_FIREFOX_PROFILE:-$HOME/.config/stream-firefox}"
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
echo "=== 2. display detection ==="
python3 stream.py --list-displays || fail=1

echo
echo "=== 3. kiosk profile ==="
if command -v firefox >/dev/null || command -v firefox-esr >/dev/null; then
    echo "OK: $(firefox --version 2>/dev/null || firefox-esr --version)"
else
    echo "FAIL: firefox is not installed (sudo apt install firefox)"
    fail=1
fi
if [ -d "$PROFILE" ]; then
    echo "OK: profile $PROFILE"
else
    echo "FAIL: no kiosk profile at $PROFILE - run ./setup-firefox.sh"
    fail=1
fi
if ls "$PROFILE"/extensions/*.xpi >/dev/null 2>&1; then
    echo "OK: uBlock Origin sideloaded"
else
    echo "FAIL: no add-on in $PROFILE/extensions - run ./setup-firefox.sh"
    fail=1
fi

echo
echo "=== 4. site handling ==="
python3 stream.py --list-sites || fail=1
# Whichever site class is first, forced onto a placeholder URL: this checks that
# a launch can be built, without naming any site's pages here.
first_site=$(python3 -c "from streamplayer import sites; print(sites.SITES[0].name if sites.SITES else '')" 2>/dev/null)
if [ -z "$first_site" ]; then
    echo "FAIL: no site classes found in streamplayer/sites/"
    fail=1
elif python3 stream.py --dry-run --site "$first_site" 'https://example.com/verify' >/dev/null 2>&1; then
    echo "OK: the $first_site site class builds a launch"
else
    echo "FAIL: could not build a launch - see: python3 stream.py --dry-run --site $first_site 'https://example.com/verify'"
    fail=1
fi

echo
echo "=== 5. web control panel ==="
if python3 -c "
from webapp import catalog
c = catalog.load()
if c.error:
    raise SystemExit('streams.json: ' + c.error)
print(f'OK: {len(c.streams)} stream(s) in {c.path}')
for w in c.warnings:
    print('WARN:', w)
for s in c.streams:
    if not s.handler:
        print(f'WARN: {s.id}: no site class handles {s.url}')
"; then
    :
else
    echo "FAIL: the stream catalog is unusable - fix streams.json"
    fail=1
fi
if python3 -c "
from webapp import config
c = config.load()
if c.error:
    raise SystemExit('config.json: ' + c.error)
if c.has_credentials:
    print('OK: Advanced tab login configured')
else:
    print('NOTE: no login in config.json - the Advanced tab stays locked'
          ' (cp config.example.json config.json)')
print('OK: playback defaults', c.defaults)
"; then
    :
else
    echo "FAIL: config.json is unusable - fix it or delete it"
    fail=1
fi
if systemctl --user is-active --quiet stream-web.service; then
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:5000/ || echo 000)"
    if [ "$code" = "200" ]; then
        echo "OK: panel answering on http://$(hostname):5000"
    else
        echo "FAIL: stream-web.service is running but / returned $code"
        fail=1
    fi
else
    echo "NOTE: stream-web.service is not running (systemctl --user status stream-web.service)"
fi

if [ -z "$URL" ]; then
    echo
    echo "(pass a stream URL to also test real playback: ./verify.sh '<url>')"
else
    echo
    echo "=== 6. real playback, 45 seconds on the TV ==="
    out="$(timeout --signal=TERM 45 python3 stream.py --retries 0 "$URL" 2>&1)"
    echo "$out" | grep -E 'playing fullscreen|gave up|no video|ERROR' || true
    if echo "$out" | grep -q 'playing fullscreen'; then
        echo "OK: the video played fullscreen"
    else
        echo "FAIL: the video did not reach fullscreen playback - rerun with -v"
        fail=1
    fi
fi

echo
if [ "$fail" -eq 0 ]; then
    echo "ALL CHECKS PASSED"
else
    echo "SOME CHECKS FAILED (exit $fail)"
fi
exit "$fail"
