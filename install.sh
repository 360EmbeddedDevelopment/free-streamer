#!/usr/bin/env bash
# One-shot setup: takes a fresh Raspberry Pi from `git clone` to a control panel
# that comes back after every reboot.
#
#   ./install.sh                       everything (asks about your screen)
#   ./install.sh --reconfigure-auth    just reset the Advanced-tab login
#   ./install.sh --reconfigure-display just re-answer the screen questions
#   ./install.sh --no-packages --no-cursor --no-service      pick and choose
#
# Nothing about any particular Pi or TV is built in. What the install needs to
# know about this one - which HDMI port the TV is on, what resolution to play
# at - it detects and asks, and keeps the answers in config.json.
#
# Safe to re-run: every step checks before it acts, so running it again after a
# git pull upgrades the box rather than breaking it.
#
# What it does NOT do: change your login settings. If the desktop does not log in
# automatically the panel cannot start at boot, so that gets reported, not fixed.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
# wlr-randr: the player sets the screen's resolution through it at each launch.
PACKAGES=(firefox python3-flask curl wlr-randr)
WALLPAPER="$REPO/images/desktop_background.jpg"

do_packages=1
do_firefox=1
do_config=1
do_wallpaper=1
do_cursor=1
do_display=1
do_service=1
do_verify=1
auth_only=0
display_only=0

for arg in "$@"; do
  case "$arg" in
    --no-packages)  do_packages=0 ;;
    --no-firefox)   do_firefox=0 ;;
    --no-wallpaper) do_wallpaper=0 ;;
    --no-cursor)    do_cursor=0 ;;
    --no-display)   do_display=0 ;;
    --no-service)   do_service=0 ;;
    --no-verify)    do_verify=0 ;;
    --reconfigure-auth) auth_only=1 ;;
    --reconfigure-display) display_only=1 ;;
    -h|--help) sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
ok()   { printf '   OK   %s\n' "$1"; }
note() { printf '   ..   %s\n' "$1"; }
warn() { printf '   WARN %s\n' "$1"; }

# ask "question" y|n - true for yes. Without a terminal (piped, or run by a
# script) there is nobody to answer, so the default stands and says so, rather
# than a bare `read` hitting end-of-input and set -e killing the install.
ask() {
  local question=$1 default=$2 hint reply
  [ "$default" = y ] && hint="[Y/n]" || hint="[y/N]"
  if [ ! -t 0 ]; then
    note "$question $hint - no terminal, taking the default ($default)"
    [ "$default" = y ]
    return
  fi
  read -rp "   $question $hint " reply || reply=""
  [[ "${reply:-$default}" =~ ^[Yy] ]]
}

# -- config.json ---------------------------------------------------------
#
# The password lands in this file in plain text - see the header of
# webapp/config.py - so it is written 0600 and never echoed.

write_auth() {
  local user pass pass2
  read -rp "   Advanced-tab username: " user
  [ -n "$user" ] || { warn "empty username - skipping the login"; return 1; }
  read -rsp "   Password: " pass; echo
  read -rsp "   Again:    " pass2; echo
  if [ "$pass" != "$pass2" ]; then
    warn "passwords did not match - leaving the login unset"
    return 1
  fi
  [ -n "$pass" ] || { warn "empty password - leaving the login unset"; return 1; }

  # json.dump, not sed: a password may contain " or \ and must survive intact.
  USER="$user" PASS="$pass" python3 - "$REPO/config.json" <<'PY'
import json, os, sys

path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
except (FileNotFoundError, ValueError):
    data = {}
data.pop("_comment", None)
data["auth"] = {"username": os.environ["USER"], "password": os.environ["PASS"]}
data.setdefault("defaults", {
    "autoplay": True, "retries": -1, "backend": "auto", "profile": "", "firefox_args": [],
})
with open(path, "w", encoding="utf-8") as handle:
    json.dump(data, handle, indent=2)
    handle.write("\n")
PY
  chmod 600 "$REPO/config.json"
  ok "login saved to config.json (0600)"
}

# -- the screen ----------------------------------------------------------
#
# Which HDMI port the TV is on, and what resolution to play at. Detected with the
# player's own code, asked where there is a real choice, and saved as the
# "display" section of config.json - which the player reads at every launch, so
# nothing about this Pi's screen is written into the project itself.

configure_display() {
  local rows=() name best has1080 connector="" mode="" pick i height
  mapfile -t rows < <(cd "$REPO" && python3 -c "
from streamplayer import display
for c in display.connected_connectors():
    print(c.name, display.best_mode(c) or '-', 'yes' if '1920x1080' in c.modes else 'no')
" 2>/dev/null)

  if [ "${#rows[@]}" -eq 0 ]; then
    warn "no display found on any HDMI port - is the TV on and plugged in?"
    note "the player will use whichever port has a display when a stream starts"
    best="-"; has1080=unknown
  elif [ "${#rows[@]}" -eq 1 ]; then
    read -r name best has1080 <<< "${rows[0]}"
    ok "display found on $name (largest mode $best)"
    note "not pinning the port: the player follows whichever one the TV is on"
  else
    echo "   Displays are connected to more than one HDMI port:"
    for i in "${!rows[@]}"; do
      read -r name best has1080 <<< "${rows[$i]}"
      echo "     $((i + 1))) $name  (largest mode $best)"
    done
    pick=1
    if [ -t 0 ]; then
      read -rp "   Which one is the TV the streams should play on? [1-${#rows[@]}, default 1] " pick || pick=1
    fi
    [[ "$pick" =~ ^[0-9]+$ ]] && [ "$pick" -ge 1 ] && [ "$pick" -le "${#rows[@]}" ] || pick=1
    read -r name best has1080 <<< "${rows[$((pick - 1))]}"
    connector=$name
    ok "streams will play on $name"
  fi

  # Streams top out at 1080p. Scaling them to a 4K screen is more than the Pi's
  # GPU manages sixty times a second - it drops most frames - while the TV does
  # that upscaling itself, for free.
  height=${best#*x}
  if [ "$best" = "-" ]; then
    if ask "If the screen turns out to be bigger than 1080p, play at 1920x1080 (recommended)?" y; then
      mode=1920x1080
    fi
  elif [ "$height" -gt 1080 ] 2>/dev/null && [ "$has1080" = yes ]; then
    echo "   This screen can run at $best, but streams are at most 1080p, and the Pi"
    echo "   cannot scale video up to $best smoothly. At 1920x1080 the TV upscales it"
    echo "   itself, with no loss - the difference is roughly 20 frames a second vs 60."
    if ask "Switch the screen to 1920x1080 when a stream starts (recommended)?" y; then
      mode=1920x1080
    fi
  elif [ "$height" -gt 1080 ] 2>/dev/null; then
    note "this screen does not offer 1920x1080, so it stays at $best"
  else
    ok "the screen is 1080p or smaller already - nothing to change"
  fi

  CONNECTOR="$connector" MODE="$mode" python3 - "$REPO/config.json" <<'PY2'
import json, os, sys
path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
except (FileNotFoundError, ValueError):
    data = {}
data["display"] = {"connector": os.environ["CONNECTOR"], "mode": os.environ["MODE"]}
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
PY2
  chmod 600 "$REPO/config.json"
  ok "saved to config.json: port ${connector:-auto}, resolution ${mode:-left as it is}"
  [ -z "$mode" ] || note "applied each time a stream starts, so it survives reboots"
}

if [ "$display_only" = 1 ]; then
  step "Screen"
  if [ ! -f "$REPO/config.json" ]; then
    cp "$REPO/config.example.json" "$REPO/config.json"
    chmod 600 "$REPO/config.json"
  fi
  configure_display
  exit 0
fi

if [ "$auth_only" = 1 ]; then
  step "Advanced-tab login"
  write_auth || true
  echo
  echo "Reload the settings page to sign in."
  exit 0
fi

echo "Installing the stream panel from $REPO"

# -- 1. preflight --------------------------------------------------------

step "Checks"
if [ "$(id -u)" -eq 0 ]; then
  echo "   Do not run this as root: the panel runs as a *user* service, tied to" >&2
  echo "   the desktop session that owns the TV. Run it as the desktop user." >&2
  exit 1
fi
ok "running as $USER (uid $(id -u))"

if ! systemctl --user show-environment >/dev/null 2>&1; then
  warn "no systemd --user session reachable; the service step will be skipped"
  do_service=0
else
  ok "systemd --user is reachable"
fi

# -- 2. packages ---------------------------------------------------------

if [ "$do_packages" = 1 ]; then
  step "System packages"
  missing=()
  for pkg in "${PACKAGES[@]}"; do
    if dpkg -s "$pkg" >/dev/null 2>&1; then
      ok "$pkg"
    else
      missing+=("$pkg")
      note "$pkg is missing"
    fi
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    echo "   Installing: ${missing[*]}"
    # python3-flask, not pip: webapp imports the system Flask, and the unit's
    # ExecStart runs /usr/bin/python3. A venv would need both changed.
    sudo apt-get update
    sudo apt-get install -y "${missing[@]}"
    ok "installed ${missing[*]}"
  fi
fi

# -- 3. kiosk Firefox profile -------------------------------------------

if [ "$do_firefox" = 1 ]; then
  step "Kiosk Firefox profile and uBlock Origin"
  "$REPO/setup-firefox.sh"
fi

# -- 4. config.json ------------------------------------------------------

if [ "$do_config" = 1 ]; then
  step "config.json"
  if [ -f "$REPO/config.json" ]; then
    ok "already present - keeping it"
    if python3 -c "
from webapp import config
raise SystemExit(0 if config.load().has_credentials else 1)
" 2>/dev/null; then
      ok "Advanced-tab login is configured"
    else
      note "no login set, so the Advanced tab stays locked"
      if [ -t 0 ] && ask "Set one now?" n; then write_auth || true; fi
    fi
    # An older config.json may predate this; the password is plain text.
    [ "$(stat -c %a "$REPO/config.json")" = "600" ] || {
      chmod 600 "$REPO/config.json"
      ok "tightened config.json to 0600 (it holds a plain-text password)"
    }
  else
    cp "$REPO/config.example.json" "$REPO/config.json"
    chmod 600 "$REPO/config.json"
    ok "created from config.example.json"
    echo "   The Advanced tab (playback defaults, maintenance, reboot) needs a login."
    if [ -t 0 ]; then
      if ask "Set one now?" y; then write_auth || true; fi
    else
      note "no terminal to type a password into - run ./install.sh --reconfigure-auth later"
    fi
  fi
fi

if [ "$do_config" = 1 ]; then
  step "streams.json"
  # The real links live only on the Pi - the file is gitignored - so a fresh
  # clone starts from the example and gets its buttons from the Streams tab.
  if [ -f "$REPO/streams.json" ]; then
    ok "already present - keeping it"
  else
    cp "$REPO/streams.example.json" "$REPO/streams.json"
    ok "created from streams.example.json - add your streams on the Streams tab"
  fi
fi

# -- 5. desktop background ----------------------------------------------

if [ "$do_wallpaper" = 1 ]; then
  step "Desktop background"
  if ! ask "Use the project's picture as this Pi's desktop background?" y; then
    note "leaving the desktop background alone"
  elif [ ! -f "$WALLPAPER" ]; then
    warn "no image at $WALLPAPER - skipping"
  else
    # pcmanfm keeps one conf per output, named for it, e.g.
    # desktop-items-HDMI-A-2.conf. Glob rather than guessing the output.
    shopt -s nullglob
    confs=("$HOME/.config/pcmanfm"/*/desktop-items-*.conf)
    shopt -u nullglob

    for conf in "${confs[@]}"; do
      [ -f "$conf.bak" ] || { cp "$conf" "$conf.bak"; note "backed up $(basename "$conf")"; }
    done

    # Over SSH there is no display to talk to, and pcmanfm says so on stdout.
    if pcmanfm --set-wallpaper="$WALLPAPER" --wallpaper-mode=crop >/dev/null 2>&1; then
      ok "set via pcmanfm"
    else
      # Headless (over SSH) there is no session to talk to; write the conf
      # directly and let it take effect at the next login.
      for conf in "${confs[@]}"; do
        sed -i "s|^wallpaper=.*|wallpaper=$WALLPAPER|; s|^wallpaper_mode=.*|wallpaper_mode=crop|" "$conf"
        ok "wrote $(basename "$conf")"
      done
      [ "${#confs[@]}" -gt 0 ] || warn "no pcmanfm desktop config found - log in once, then re-run"
      note "takes effect at the next desktop login"
    fi
  fi
fi

# -- 6. the mouse pointer ------------------------------------------------

if [ "$do_cursor" = 1 ]; then
  step "Mouse pointer"
  # A Pi driving a TV rarely has a mouse, but the compositor draws a pointer on
  # the desktop behind the stream anyway. It is a desktop-wide change - a mouse
  # plugged in later has nothing to show - so it is asked, not assumed.
  if ask "Hide the mouse pointer (choose yes if this Pi only drives a TV)?" y; then
    "$REPO/hide-cursor.sh"
  else
    note "leaving the pointer as it is"
  fi
fi

# -- 7. the screen -------------------------------------------------------

if [ "$do_display" = 1 ]; then
  step "Screen"
  configure_display
fi

# -- 8. the service ------------------------------------------------------

if [ "$do_service" = 1 ]; then
  step "Control panel service"
  mkdir -p "$UNIT_DIR" "$HOME/.local/state/stream"
  sed -e "s|@REPO@|$REPO|g" -e "s|@UID@|$(id -u)|g" \
      "$REPO/systemd/stream-web.service.in" > "$UNIT_DIR/stream-web.service"
  ok "rendered $UNIT_DIR/stream-web.service"

  # The user manager must exist at boot, not just after someone logs in.
  if [ "$(loginctl show-user "$USER" --property=Linger --value 2>/dev/null)" = "yes" ]; then
    ok "lingering already enabled"
  elif loginctl enable-linger "$USER" 2>/dev/null; then
    ok "enabled lingering so the panel starts at boot"
  else
    sudo loginctl enable-linger "$USER" && ok "enabled lingering (via sudo)"
  fi

  # Older installs were wanted by graphical-session.target, which never
  # activates here; disable first so that stale symlink goes.
  systemctl --user disable stream-web.service >/dev/null 2>&1 || true
  systemctl --user daemon-reload
  systemctl --user enable --now stream-web.service
  sleep 2
  if systemctl --user is-active --quiet stream-web.service; then
    ok "stream-web.service is running and enabled at login"
  else
    warn "stream-web.service did not come up - systemctl --user status stream-web.service"
  fi

  # Prove the wiring rather than assuming it: the unit must hang off
  # default.target, which is what actually gets reached at boot here.
  if [ -L "$UNIT_DIR/default.target.wants/stream-web.service" ]; then
    ok "wired to default.target - it will start at boot"
  else
    warn "not linked into default.target.wants - it will NOT start at boot"
  fi

  # Playing a stream needs the desktop, even though serving the page does not.
  if grep -rqs "autologin" /etc/systemd/system/getty@tty1.service.d/ 2>/dev/null \
     || grep -rqsE "^\s*autologin-user=" /etc/lightdm/lightdm.conf 2>/dev/null; then
    ok "the Pi logs in automatically, so the TV has a desktop to draw on"
  else
    warn "no autologin detected. The panel will still come up, but nothing can"
    warn "play until someone logs in and the compositor owns the screen:"
    warn "  sudo raspi-config  ->  System Options  ->  Boot / Auto Login  ->  Desktop Autologin"
  fi
fi

# -- 9. check it ---------------------------------------------------------

if [ "$do_verify" = 1 ]; then
  step "Verifying"
  "$REPO/verify.sh" || true
fi

cat <<EOF

Done. The panel is at  http://$(hostname):5000  (or http://$(hostname).local:5000)

  systemctl --user status stream-web.service     is it running
  ./verify.sh                                    re-check everything
  ./install.sh --reconfigure-auth                change the Advanced-tab login
EOF
