#!/usr/bin/env bash
# Take the mouse pointer off the TV.
#
# A Pi driving a TV usually has no mouse, but the compositor draws a pointer
# anyway, and it sits on the desktop behind the stream between matches. Neither
# desktop Raspberry Pi OS ships can simply hide it - wayfire's [input] section
# offers cursor_theme, cursor_size and two speeds, and that is all - so the way
# to hide it is a cursor theme whose every cursor is one transparent pixel,
# pointed at from whichever compositor is installed: wayfire, labwc, or both.
#
# Safe to re-run: the theme is rebuilt and each config line is replaced rather
# than appended.
set -euo pipefail

THEME_NAME="blank"
THEME_DIR="$HOME/.local/share/icons/$THEME_NAME"
WAYFIRE_INI="$HOME/.config/wayfire.ini"

ok()   { printf '   OK   %s\n' "$1"; }
note() { printf '   ..   %s\n' "$1"; }
warn() { printf '   WARN %s\n' "$1"; }

# -- the theme -----------------------------------------------------------

rm -rf "$THEME_DIR"
mkdir -p "$THEME_DIR/cursors"

cat > "$THEME_DIR/index.theme" <<EOF
[Icon Theme]
Name=$THEME_NAME
Comment=Fully transparent cursors, for a screen with no mouse on it
EOF

# An Xcursor file is a short binary format, and xcursorgen is not installed on
# Pi OS, so write it directly: one 1x1 fully transparent image per nominal size
# so libxcursor finds a match whatever size is asked for.
python3 - "$THEME_DIR/cursors/left_ptr" <<'PY'
import struct, sys

SIZES = (16, 24, 32, 48, 64, 96, 128)
IMAGE_TYPE = 0xFFFD0002
CHUNK_HEADER = 36
PIXELS = struct.pack("<I", 0)  # one transparent ARGB pixel

path = sys.argv[1]
toc_start = 16
chunk_start = toc_start + 12 * len(SIZES)

header = b"Xcur" + struct.pack("<III", 16, 0x00010000, len(SIZES))
toc, chunks, offset = b"", b"", chunk_start
for size in SIZES:
    toc += struct.pack("<III", IMAGE_TYPE, size, offset)
    # header, type, subtype(size), version, w, h, xhot, yhot, delay
    chunks += struct.pack("<IIIIIIIII", CHUNK_HEADER, IMAGE_TYPE, size, 1, 1, 1, 0, 0, 0)
    chunks += PIXELS
    offset += CHUNK_HEADER + len(PIXELS)

with open(path, "wb") as out:
    out.write(header + toc + chunks)
PY

# Every name anything is likely to ask for. A name the theme does not carry
# falls back to the system theme and a real pointer reappears, so the list is
# deliberately long rather than minimal.
NAMES=(
  default arrow top_left_arrow left_ptr right_ptr X_cursor
  pointer hand hand1 hand2 pointing_hand
  text xterm ibeam vertical-text
  crosshair cross cross_reverse tcross
  progress watch left_ptr_watch wait half-busy
  help question_arrow whats_this
  not-allowed forbidden crossed_circle no-drop dnd-no-drop circle
  copy alias link dnd-copy dnd-move dnd-link dnd-none
  move fleur grab grabbing openhand closedhand all-scroll size_all
  n-resize s-resize e-resize w-resize
  ne-resize nw-resize se-resize sw-resize
  ew-resize ns-resize nesw-resize nwse-resize
  col-resize row-resize split_h split_v
  size_hor size_ver size_bdiag size_fdiag
  sb_h_double_arrow sb_v_double_arrow sb_left_arrow sb_right_arrow
  sb_up_arrow sb_down_arrow
  top_side bottom_side left_side right_side
  top_left_corner top_right_corner bottom_left_corner bottom_right_corner
  cell context-menu zoom-in zoom-out center_ptr plus draft_large draft_small
)
for name in "${NAMES[@]}"; do
  [ "$name" = "left_ptr" ] || ln -sf left_ptr "$THEME_DIR/cursors/$name"
done
ok "built $THEME_DIR (${#NAMES[@]} cursor names, all transparent)"

# libxcursor's search path differs by version; ~/.icons is the one every
# version looks in, so point it at the same theme rather than building twice.
mkdir -p "$HOME/.icons"
ln -sfn "$THEME_DIR" "$HOME/.icons/$THEME_NAME"
ok "linked ~/.icons/$THEME_NAME"

configured=0

# -- wayfire -------------------------------------------------------------

if [ -f "$WAYFIRE_INI" ]; then
[ -f "$WAYFIRE_INI.bak" ] || { cp "$WAYFIRE_INI" "$WAYFIRE_INI.bak"; note "backed up wayfire.ini"; }

THEME="$THEME_NAME" python3 - "$WAYFIRE_INI" <<'PY'
import os, re, sys

path, theme = sys.argv[1], os.environ["THEME"]
text = open(path, encoding="utf-8").read()

# "[input]" exactly - the file is full of "[input-device:...]" sections that
# must not be mistaken for it.
match = re.search(r"^\[input\][^\n]*$", text, re.M)
if match:
    start = match.end()
    end = re.search(r"^\[", text[start:], re.M)
    end = start + (end.start() if end else len(text) - start)
    body = text[start:end]
    # [ \t]* rather than \s*: \s matches a newline, so ^\s* reaches back past
    # the line start and swallows it, gluing the value onto the section header.
    if re.search(r"^[ \t]*cursor_theme[ \t]*=", body, re.M):
        body = re.sub(r"^[ \t]*cursor_theme[ \t]*=.*$", f"cursor_theme = {theme}", body, flags=re.M)
    else:
        body = "\n" + f"cursor_theme = {theme}" + body
    text = text[:start] + body + text[end:]
else:
    text = text.rstrip("\n") + f"\n\n[input]\ncursor_theme = {theme}\n"

open(path, "w", encoding="utf-8").write(text)
PY
ok "wayfire.ini: [input] cursor_theme = $THEME_NAME"
configured=1
fi

# -- labwc ---------------------------------------------------------------
#
# labwc takes the theme from XCURSOR_THEME in its environment file. Pi OS sets
# it in /etc/xdg/labwc/environment next to settings the session needs, and
# labwc may read only the first environment file it finds - so the user file is
# started as a copy of the system one, never as a file with just this line.

if command -v labwc >/dev/null 2>&1; then
  LABWC_ENV="$HOME/.config/labwc/environment"
  mkdir -p "$(dirname "$LABWC_ENV")"
  if [ ! -f "$LABWC_ENV" ] && [ -f /etc/xdg/labwc/environment ]; then
    cp /etc/xdg/labwc/environment "$LABWC_ENV"
    note "started ~/.config/labwc/environment from the system one"
  fi
  touch "$LABWC_ENV"
  [ -f "$LABWC_ENV.bak" ] || cp "$LABWC_ENV" "$LABWC_ENV.bak"
  if grep -q '^XCURSOR_THEME=' "$LABWC_ENV"; then
    sed -i "s/^XCURSOR_THEME=.*/XCURSOR_THEME=$THEME_NAME/" "$LABWC_ENV"
  else
    printf 'XCURSOR_THEME=%s\n' "$THEME_NAME" >> "$LABWC_ENV"
  fi
  ok "labwc environment: XCURSOR_THEME=$THEME_NAME"
  configured=1
fi

if [ "$configured" = 0 ]; then
  warn "found neither a wayfire.ini nor labwc - log into the desktop once, then re-run this"
  exit 0
fi
note "takes effect at the next login (wayfire may pick it up live)"
