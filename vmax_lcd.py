#!/usr/bin/env python3
"""Drive an "HL VMAX" PC-case LCD (USB 33c3:f101, CDC-ACM serial) on Linux.

Protocol (as documented by the open-source solarmax-cm-b600l-panel, InfoPanel
and vdisplay projects for this USB ID):
  * open the CDC serial port (baud is ignored by the firmware), DTR/RTS high
  * handshake: send F0 A5 5A 0F, panel answers ~26 ASCII bytes such as
    "VMAXB160462*1920S261301227"  -> 7-char model, width, '*', height, serial
  * frame: one baseline JPEG at exactly the native (portrait) resolution,
    written as-is with no header
  * resend the frame about every second, otherwise the panel falls back to
    its built-in boot animation

Images live in  image/<WIDTH>x<HEIGHT>/  next to this script; the folder
matching the connected panel's resolution is used automatically.

Only depends on the standard library and Pillow (plus GTK 4 for the UI).
"""

import argparse
import fcntl
import glob
import json
import os
import re
import select
import struct
import sys
import termios
import threading
import time
from io import BytesIO
from pathlib import Path

try:
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover
    sys.exit("Pillow is required: sudo dnf install python3-pillow")

VID, PID = "33c3", "f101"
HANDSHAKE = bytes([0xF0, 0xA5, 0x5A, 0x0F])
IDENT_RE = re.compile(rb"^(.{7})(\d{3,4})\*(\d{3,4})(\S*)")

APP_DIR = Path(__file__).resolve().parent
IMAGE_ROOT = APP_DIR / "image"
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
STATE_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) \
    / "vmax-lcd" / "state.json"

TIOCEXCL = 0x540C
TIOCMBIS = 0x5416
TIOCM_DTR = 0x002
TIOCM_RTS = 0x004


class PanelError(Exception):
    pass


# ---------------------------------------------------------------- device ---

def find_port():
    """Locate the panel's tty by USB VID:PID."""
    for tty in sorted(glob.glob("/sys/class/tty/ttyACM*")):
        usbdev = os.path.dirname(os.path.realpath(os.path.join(tty, "device")))
        try:
            vid = Path(usbdev, "idVendor").read_text().strip()
            pid = Path(usbdev, "idProduct").read_text().strip()
        except OSError:
            continue
        if (vid, pid) == (VID, PID):
            return "/dev/" + os.path.basename(tty)
    return None


class Panel:
    def __init__(self, port=None):
        self.port = port or find_port()
        if not self.port:
            raise PanelError(f"No HL VMAX panel ({VID}:{PID}) found.")
        try:
            self.fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except PermissionError:
            raise PanelError(
                f"Permission denied opening {self.port}. Install "
                "70-vmax-lcd.rules (see README.md).") from None
        except OSError as e:
            raise PanelError(f"Cannot open {self.port}: {e.strerror}") from None
        try:
            fcntl.ioctl(self.fd, TIOCEXCL)  # keep ModemManager & co. out
        except OSError:
            pass
        attrs = termios.tcgetattr(self.fd)
        attrs[0] = attrs[1] = attrs[3] = 0             # raw i/o/l flags
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[4] = attrs[5] = termios.B115200          # ignored by firmware
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        fcntl.ioctl(self.fd, TIOCMBIS, struct.pack("I", TIOCM_DTR | TIOCM_RTS))
        termios.tcflush(self.fd, termios.TCIOFLUSH)

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass

    def write(self, data, timeout=8.0):
        view = memoryview(data)
        deadline = time.monotonic() + timeout
        while view:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("write to panel timed out")
            _, w, _ = select.select([], [self.fd], [], remaining)
            if w:
                view = view[os.write(self.fd, view):]
        termios.tcdrain(self.fd)

    def read_burst(self, first_timeout=1.0, quiet=0.15, limit=64):
        """Read until the line goes quiet after the first byte."""
        buf = b""
        timeout = first_timeout
        while len(buf) < limit:
            r, _, _ = select.select([self.fd], [], [], timeout)
            if not r:
                break
            chunk = os.read(self.fd, limit - len(buf))
            if not chunk:
                break
            buf += chunk
            timeout = quiet
        return buf

    def identify(self, retries=3):
        for _ in range(retries):
            termios.tcflush(self.fd, termios.TCIFLUSH)
            self.write(HANDSHAKE)
            reply = self.read_burst()
            if reply:
                return reply
            time.sleep(1.0)
        return b""

    def info(self):
        reply = self.identify()
        info = parse_ident(reply)
        if not info:
            raise PanelError(f"Unrecognised identity reply: {reply!r}")
        return info

    def set_brightness(self, value):
        # Only documented by one project (solarmax); untested on other SKUs.
        value = max(0, min(100, int(value)))
        self.write(bytes([0xAA, 0xBB, value, 0xCC, 0xDD]))


def parse_ident(reply):
    m = IDENT_RE.match(reply)
    if not m:
        return None
    return {
        "model": m.group(1).decode("ascii", "replace"),
        "width": int(m.group(2)),
        "height": int(m.group(3)),
        "serial": m.group(4).decode("ascii", "replace"),
    }


class Streamer(threading.Thread):
    """Holds the panel open and keeps re-sending the current frame.

    Reconnects automatically (e.g. after the panel is replugged).
    Callbacks run on this thread.
    """

    BACKOFF = (1, 2, 5, 10)

    def __init__(self, port=None, interval=1.0, on_connect=None,
                 on_status=None, on_error=None):
        super().__init__(daemon=True)
        self.port = port
        self.interval = interval
        self.on_connect = on_connect or (lambda info: None)
        self.on_status = on_status or (lambda msg: None)
        self.on_error = on_error or (lambda msg: None)
        self.info = None
        self._frame = None
        self._dirty = False
        self._stopping = False
        self._cond = threading.Condition()

    def set_frame(self, jpeg):
        """Show this JPEG (None = stop sending; panel reverts to animation)."""
        with self._cond:
            self._frame = jpeg
            self._dirty = True
            self._cond.notify()

    def stop(self):
        with self._cond:
            self._stopping = True
            self._cond.notify()
        if self.is_alive():
            self.join(timeout=10)

    def _sleep(self, seconds):
        with self._cond:
            if not self._stopping:
                self._cond.wait(seconds)

    def run(self):
        fails = 0
        while not self._stopping:
            panel = None
            try:
                panel = Panel(self.port)
                self.info = panel.info()
                fails = 0
                self.on_connect(self.info)
                with self._cond:
                    self._dirty = self._frame is not None
                while True:
                    with self._cond:
                        if not self._dirty and not self._stopping:
                            self._cond.wait(self.interval)
                        if self._stopping:
                            break
                        frame, self._dirty = self._frame, False
                    if frame:
                        panel.write(frame)
            except (PanelError, OSError, TimeoutError) as e:
                self.info = None
                delay = self.BACKOFF[min(fails, len(self.BACKOFF) - 1)]
                fails += 1
                self.on_error(f"{e} (retrying in {delay}s)")
                self._sleep(delay)
            finally:
                if panel:
                    panel.close()


# ---------------------------------------------------------------- images ---

def geometry_key(width, height):
    return f"{width}x{height}"


def image_dir(width, height, root=None, create=True):
    d = Path(root or IMAGE_ROOT) / geometry_key(width, height)
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def list_images(folder):
    folder = Path(folder)
    if not folder.is_dir():
        return []
    return sorted((p for p in folder.iterdir()
                   if p.is_file() and p.suffix.lower() in IMAGE_EXTS
                   and not p.name.startswith(".")),
                  key=lambda p: p.name.lower())


def render_jpeg(path, width, height, rotate=0, fit="cover", quality=85,
                background="black"):
    with Image.open(path) as src:
        img = ImageOps.exif_transpose(src).convert("RGB")
    if rotate:
        img = img.rotate(-rotate, expand=True)  # positive = clockwise
    size = (width, height)
    if img.size != size:
        if fit == "cover":
            img = ImageOps.fit(img, size, Image.LANCZOS)
        elif fit == "contain":
            canvas = Image.new("RGB", size, background)
            img = ImageOps.contain(img, size, Image.LANCZOS)
            canvas.paste(img, ((width - img.width) // 2,
                               (height - img.height) // 2))
            img = canvas
        else:  # stretch
            img = img.resize(size, Image.LANCZOS)
    out = BytesIO()
    # baseline, 4:2:0 — what the panel firmware is known to accept
    img.save(out, "JPEG", quality=quality, subsampling=2,
             progressive=False, optimize=False)
    return out.getvalue()


# ----------------------------------------------------------------- state ---

def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(**changes):
    state = load_state()
    for k, v in changes.items():
        if isinstance(v, dict):
            state.setdefault(k, {}).update(v)
        else:
            state[k] = v
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except OSError:
        pass


def resolve_image(arg, folder, key):
    """Pick the image to show: explicit path, a name in the folder, the last
    one used at this resolution, or the first in the folder."""
    if arg:
        p = Path(arg).expanduser()
        if p.is_file():
            return p
        p = folder / arg
        if p.is_file():
            return p
        raise PanelError(f"Image not found: {arg} (also looked in {folder})")
    last = load_state().get("last", {}).get(key)
    if last and (folder / last).is_file():
        return folder / last
    images = list_images(folder)
    if not images:
        raise PanelError(f"No images in {folder} — add some and retry.")
    return images[0]


# ------------------------------------------------------------------- CLI ---

def connect(args):
    panel = Panel(args.port)
    if getattr(args, "size", None):
        w, h = (int(x) for x in args.size.lower().split("x"))
        return panel, {"model": "?", "width": w, "height": h, "serial": "?"}
    info = panel.info()
    print(f"Panel: {info['model']}  {info['width']}x{info['height']}  "
          f"serial {info['serial']}  ({panel.port})")
    return panel, info


def cmd_info(args):
    panel, info = connect(args)
    panel.close()
    d = image_dir(info["width"], info["height"], args.image_dir)
    print(f"Image folder: {d}")
    return 0


def cmd_list(args):
    if args.size:
        w, h = (int(x) for x in args.size.lower().split("x"))
    else:
        panel, info = connect(args)
        panel.close()
        w, h = info["width"], info["height"]
    d = image_dir(w, h, args.image_dir)
    images = list_images(d)
    print(f"{d}: {len(images)} image(s)")
    last = load_state().get("last", {}).get(geometry_key(w, h))
    for p in images:
        print(("* " if p.name == last else "  ") + p.name)
    return 0


def cmd_show(args):
    panel, info = connect(args)
    try:
        w, h = info["width"], info["height"]
        key = geometry_key(w, h)
        folder = image_dir(w, h, args.image_dir)
        path = resolve_image(args.image, folder, key)
        jpeg = render_jpeg(path, w, h, rotate=args.rotate, fit=args.fit,
                           quality=args.quality)
        print(f"Image: {path}  ->  {w}x{h} JPEG, {len(jpeg) // 1024} KiB",
              flush=True)
        if path.parent.resolve() == folder.resolve():
            save_state(last={key: path.name})
        if args.save_preview:
            Path(args.save_preview).write_bytes(jpeg)
        if args.brightness is not None:
            panel.set_brightness(args.brightness)
        panel.write(jpeg)
        if args.once:
            print("Sent once. The panel may revert to its boot animation soon.")
            return 0
        print(f"Showing; resending every {args.interval}s. Ctrl+C to stop.",
              flush=True)
        while True:
            time.sleep(args.interval)
            panel.write(jpeg)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        panel.close()
    return 0


def cmd_brightness(args):
    panel = Panel(args.port)
    try:
        panel.set_brightness(args.value)
        print(f"Brightness command sent ({args.value}).")
    finally:
        panel.close()
    return 0


def cmd_ui(args):
    from vmax_lcd_ui import run_ui
    return run_ui(port=args.port, image_root=args.image_dir)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--port", help="serial device (default: auto-detect)")
    p.add_argument("--image-dir", type=Path, default=IMAGE_ROOT,
                   help=f"image root folder (default: {IMAGE_ROOT})")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="print the panel's identity and image folder")

    lp = sub.add_parser("list", help="list images for the panel's resolution")
    lp.add_argument("--size", help="use this resolution instead of detecting")

    s = sub.add_parser("show", help="display an image on the panel")
    s.add_argument("image", nargs="?",
                   help="file path or name inside image/<WxH>/ "
                        "(default: last used, else first in folder)")
    s.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270],
                   help="rotate clockwise before fitting")
    s.add_argument("--fit", choices=["cover", "contain", "stretch"],
                   default="cover",
                   help="how to fit images that aren't the panel's exact size")
    s.add_argument("--quality", type=int, default=85)
    s.add_argument("--interval", type=float, default=1.0,
                   help="keep-alive resend interval in seconds")
    s.add_argument("--once", action="store_true",
                   help="send a single frame and exit")
    s.add_argument("--size", help="override geometry, e.g. 462x1920")
    s.add_argument("--brightness", type=int, help="0-100 (experimental)")
    s.add_argument("--save-preview", metavar="FILE",
                   help="also save the exact JPEG sent to the panel")

    b = sub.add_parser("brightness", help="set brightness 0-100 (experimental)")
    b.add_argument("value", type=int)

    sub.add_parser("ui", help="open the graphical image picker")

    args = p.parse_args()
    try:
        return {"info": cmd_info, "list": cmd_list, "show": cmd_show,
                "brightness": cmd_brightness, "ui": cmd_ui}[args.cmd](args)
    except PanelError as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    sys.exit(main())
