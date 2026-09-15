# HDMI Stream Player

Plays a stream full-screen on the TV attached to this Raspberry Pi 5's HDMI port.

Point it at sources you're authorized to watch: a direct HLS/DASH feed, an RTSP
camera, an IPTV subscription you pay for, YouTube, or a streaming service you have
an account with.

## Quick start

```bash
# Which HDMI port has the TV, and how will it be driven?
./stream.py --list-displays

# Play a direct stream
./stream.py 'https://example.com/live/stream.m3u8'

# Resolve a page with yt-dlp first
./stream.py 'https://www.youtube.com/watch?v=...'

# See exactly what would run, without playing
./stream.py --dry-run 'https://example.com/live/stream.m3u8'
```

It works over SSH — no need to sit at the Pi.

## This Pi's setup

Detected and verified on this machine:

| Item | Value |
|---|---|
| TV | `HDMI-A-2` (the **HDMI1** port), 3840x2160@60 |
| Other port | `HDMI-A-1`, nothing attached |
| Display stack | **X11** on `:0` — a desktop session owns the screen |
| Output name in X | `HDMI-2` (X drops the DRM connector letter) |
| HDMI audio | `alsa/hdmi:CARD=vc4hdmi1,DEV=0` |
| HEVC decoder | `/dev/video19` (`rpi-hevc-dec`) |

All of it is auto-detected, so normally you pass nothing but a URL.

## How it reaches the screen

There are three ways to drive the display, and `--backend auto` (the default)
picks the right one:

- **`x11`** — what this Pi uses now. A desktop session already owns the screen, so
  mpv plays as a fullscreen `--ontop` client on output `HDMI-2`. `DISPLAY` and
  `XAUTHORITY` are filled in automatically, which is why SSH works.
- **`wayland`** — same idea if you switch the Pi to a Wayland session.
- **`drm`** — for a Pi booted to a bare console with no desktop. mpv takes the
  display over directly via KMS. **This only works when nothing else owns the
  display**: DRM master is exclusive, so with the desktop running you'd get a
  "failed to become DRM master" error. Force it with `--backend drm`.

To actually switch this Pi to the console-only case: `sudo systemctl set-default
multi-user.target` and reboot. There's no need to — X works fine and is easier to
drive remotely.

## Hardware decoding

The Pi 5 has an **HEVC** hardware decoder but **no H.264** one. So:

- **HEVC** is offloaded to `/dev/video19` (`--hwdec=drm`, chosen by default when
  that device exists). Verified: a 3840x2160 HEVC clip decodes in hardware
  (`drm_prime[rpi4_8]`). Pure software 4K HEVC is *not* fast enough on this board.
- **H.264** falls back to software automatically. 1080p60 is comfortable; 4K H.264
  is the case that drops frames.

That's why `--max-height 1080` is the default. For a 4K HEVC source, lift the cap:

```bash
./stream.py --full-res 'https://example.com/live/4k-hevc.m3u8'
```

Override the decoder with `--hwdec` (`no`, `drm`, `v4l2m2m-copy`, `auto-safe`, …).
Note that `auto-safe` probes CUDA and VDPAU, which this board doesn't have, and
prints harmless loader errors on every launch — that's the reason it isn't the
default.

## Reconnecting

Live feeds drop. The supervisor re-resolves the URL (stream tokens expire) and
relaunches with exponential backoff up to 30s. A stream that played for over a
minute before stopping resets the backoff, so a normal blip reconnects quickly
while a genuinely dead source backs off instead of hot-looping. A player that dies
instantly and cleanly is reported as a bad URL rather than retried forever.

- `--retries -1` (default) — keep trying forever
- `--retries 0` — one attempt, then exit
- `--retries 5` — at most 5 restarts

`Ctrl-C` shuts the player down cleanly and restores the console cursor.

## Login-gated services

mpv can't play Widevine-protected streams, so services you sign into need the
kiosk browser:

```bash
./stream.py --player chromium 'https://www.nfl.com/plus/'
```

Since a session is already running, this is just Chromium in `--kiosk` on the TV.
The first run needs a keyboard and mouse to sign in; the profile at
`~/.config/stream-kiosk` persists, so later runs go straight there. Widevine is
installed (`/opt/WidevineCdm`), though premium services often cap Linux/ARM
playback below 4K — their limit, not the Pi's.

## Autostart on boot

The unit is provided but **not enabled**. It's a *user* service, because it needs
the desktop session that owns the TV:

```bash
# Put your URL in the ExecStart line first
nano systemd/stream.service

mkdir -p ~/.config/systemd/user
cp systemd/stream.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now stream.service

journalctl --user -u stream.service -f
```

Stop it booting into a stream again: `systemctl --user disable --now stream.service`.

## Troubleshooting

**yt-dlp fails to extract a stream.** The apt-installed `yt-dlp` here is
**2023.03.04**, years behind upstream, and YouTube extraction currently fails with
"No video formats found!". Direct `.m3u8`/`.mpd` URLs don't touch yt-dlp and work
regardless. To fix extraction:

```bash
# Option A: a standalone binary, independent of apt
sudo curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux_aarch64 \
  -o /usr/local/bin/yt-dlp && sudo chmod +x /usr/local/bin/yt-dlp

# Option B: pipx
sudo apt install pipx && pipx install yt-dlp
```

`/usr/local/bin` precedes `/usr/bin` on PATH, so option A takes effect immediately.

**No audio.** Confirm which ALSA card is really the TV — the `vc4hdmi0`/`vc4hdmi1`
numbering doesn't always follow the connector index:

```bash
speaker-test -D hdmi:CARD=vc4hdmi1,DEV=0 -c 2 -t wav   # should hiss from the TV
speaker-test -D hdmi:CARD=vc4hdmi0,DEV=0 -c 2 -t wav   # try the other if silent
```

Then pass the working one: `--audio-device 'alsa/hdmi:CARD=vc4hdmi0,DEV=0'`.

**"Failed to become DRM master."** You used `--backend drm` while the desktop is
running. Drop the flag and let auto-detection use X.

**Black screen, no errors.** Check `--dry-run` output first. Under X, verify the
output name matches `xrandr` (`DISPLAY=:0 xrandr`). Under `--backend drm`, try
forcing a lower mode: `--drm-mode 1920x1080`.

**Stuttering.** Lower the cap (`--max-height 720`), and drop `--low-latency` if you
passed it — it shrinks the buffer that smooths a jittery feed.

**Everything hangs, including `xrandr`, and mpv won't die even with SIGKILL.** The
display stack is wedged in the kernel and only a reboot clears it. This happened
once during development on this Pi, right after 4K HEVC hardware-decode playback:

```
Unable to handle kernel NULL pointer dereference at virtual address 00000000000004d1
Internal error: Oops: 0000000096000005 [#1] PREEMPT SMP
  drm_atomic_helper_unprepare_planes+0x4c/0xd8 [drm_kms_helper]
  drm_mode_cursor_universal / drm_mode_cursor2_ioctl [drm]
```

That's a NULL dereference inside the vc4 DRM cursor-plane path — a kernel/driver
bug, not something this program can cause or avoid from userspace. Symptoms: any
new GPU client (mpv, `xrandr`, a browser) blocks forever in an uninterruptible
kernel ioctl. Check for it with `sudo dmesg | grep -iE 'Oops|Unable to handle'`.
Reboot to recover. If 4K HEVC playback triggers it repeatedly on your kernel, stick
to `--max-height 1080`, or try `--hwdec no` for HEVC to keep the hardware decoder
out of the path.

## Layout

```
stream.py                   CLI entry point
streamplayer/display.py     HDMI connector, backend, audio and HEVC decoder detection
streamplayer/resolver.py    yt-dlp URL resolution
streamplayer/players.py     mpv and Chromium command builders
streamplayer/supervisor.py  retry/backoff loop, signal handling, logging
systemd/stream.service      optional autostart user unit (disabled by default)
```

Standard library only — it drives the `mpv`, `yt-dlp`, and `chromium` binaries
already installed.
