#!/usr/bin/env bash
# One-time setup for ./stream.py: builds the kiosk Firefox profile, installs
# uBlock Origin into it, and sets the prefs a TV kiosk wants.
# Re-run it any time to update uBlock Origin. Safe to re-run; it touches only
# the kiosk profile, never the Firefox profile you browse with by hand.
set -euo pipefail

PROFILE="${STREAM_FIREFOX_PROFILE:-$HOME/.config/stream-firefox}"
UBO_ID='uBlock0@raymondhill.net'
UBO_URL='https://addons.mozilla.org/firefox/downloads/latest/ublock-origin/latest.xpi'

command -v firefox >/dev/null || command -v firefox-esr >/dev/null || {
  echo "firefox is not installed (try: sudo apt install firefox)" >&2
  exit 1
}

mkdir -p "$PROFILE/extensions"

# Firefox disables an add-on dropped into a profile until someone clicks to
# approve it, and a kiosk has nobody to click. autoDisableScopes=0 turns that
# off for this profile, so the sideload below comes up enabled.
cat > "$PROFILE/user.js" <<'PREFS'
// Managed by setup-firefox.sh - edit the script, not this file.
user_pref("extensions.autoDisableScopes", 0);
// Nothing here can dismiss a first-run tour, an update page, or a default-browser prompt.
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.aboutwelcome.enabled", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("datareporting.policy.dataSubmissionPolicyBypassNotification", true);
// A stream should start on its own, with sound.
user_pref("media.autoplay.default", 0);
user_pref("media.autoplay.blocking_policy", 0);
// Skip the "you are now fullscreen" overlay when a player goes fullscreen itself.
user_pref("full-screen-api.warning.timeout", 0);
// Let the site driver put the video fullscreen itself. Fullscreen normally
// requires a click to have happened; there is nobody here to click.
// streamplayer/firefox.py rewrites this on every launch too, so a profile built
// before this line existed still works.
user_pref("full-screen-api.allow-trusted-requests-only", false);
// These pages beg for notification permission on load, and a kiosk cannot
// answer the prompt. 2 = deny without asking.
user_pref("permissions.default.desktop-notification", 2);
user_pref("dom.push.enabled", false);
PREFS

echo "installing uBlock Origin into $PROFILE ..."
tmp="$(mktemp)"
curl -fsSL -o "$tmp" "$UBO_URL"
mv "$tmp" "$PROFILE/extensions/$UBO_ID.xpi"
chmod 644 "$PROFILE/extensions/$UBO_ID.xpi"

# Add-on registration is cached, and an add-on already recorded as disabled
# stays disabled however the prefs change. Dropping the caches makes Firefox
# rescan the extensions directory on the next start. No cookies, logins or
# history live in these files.
rm -f "$PROFILE/extensions.json" "$PROFILE/addonStartup.json.lz4"

ver="$(python3 -c "
import json, sys, zipfile
z = zipfile.ZipFile('$PROFILE/extensions/$UBO_ID.xpi')
print(json.loads(z.read('manifest.json'))['version'])
")"
echo "uBlock Origin $ver installed"
echo
echo "Now run:  ./stream.py '<url>'"
