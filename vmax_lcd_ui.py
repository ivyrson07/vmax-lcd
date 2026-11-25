#!/usr/bin/env python3
"""GTK 4 image picker for the HL VMAX case LCD.

Shows the images in image/<WxH>/ for the connected panel as a grid; click one
to display it. The panel is kept alive for as long as the window is open.
"""

import shutil
import signal
import sys
import threading
from io import BytesIO
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402
from PIL import Image, ImageOps  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vmax_lcd as core  # noqa: E402

THUMB_H = 300
FITS = ["cover", "contain", "stretch"]
ROTATIONS = [0, 90, 180, 270]


def make_texture(path, w, h):
    with Image.open(path) as src:
        img = ImageOps.exif_transpose(src).convert("RGB")
    img.thumbnail((w * 2, h * 2))
    buf = BytesIO()
    img.save(buf, "PNG")
    return Gdk.Texture.new_from_bytes(GLib.Bytes.new(buf.getvalue()))


class Window(Gtk.ApplicationWindow):
    def __init__(self, app, port, image_root):
        super().__init__(application=app, title="VMAX LCD")
        self.set_default_size(960, 720)
        self.image_root = Path(image_root)
        self.info = None
        self.folder = None
        self.current = None          # Path being shown
        self.paused = False
        self._monitor = None
        self._refresh_pending = False
        self._render_token = 0
        state = core.load_state()
        self.fit = state.get("fit", "cover")
        self.rotate = state.get("rotate", 0)

        self._build_ui()

        self.streamer = core.Streamer(
            port=port,
            on_connect=lambda info: GLib.idle_add(self._on_connect, info),
            on_error=lambda msg: GLib.idle_add(self._on_error, msg),
        )
        self.streamer.start()
        self.connect("close-request", self._on_close)

    # ------------------------------------------------------------ layout --
    def _build_ui(self):
        header = Gtk.HeaderBar()
        self.set_titlebar(header)

        add_btn = Gtk.Button(icon_name="list-add-symbolic",
                             tooltip_text="Add images to the folder")
        add_btn.connect("clicked", self._on_add)
        header.pack_start(add_btn)

        folder_btn = Gtk.Button(icon_name="folder-open-symbolic",
                                tooltip_text="Open the image folder")
        folder_btn.connect("clicked", self._on_open_folder)
        header.pack_start(folder_btn)

        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic",
                                 tooltip_text="Reload images")
        refresh_btn.connect("clicked", lambda *_: self.refresh())
        header.pack_start(refresh_btn)

        self.pause_btn = Gtk.ToggleButton(
            icon_name="media-playback-pause-symbolic",
            tooltip_text="Stop sending (panel shows its boot animation)")
        self.pause_btn.connect("toggled", self._on_pause)
        header.pack_end(self.pause_btn)

        rot = Gtk.DropDown.new_from_strings([f"{r}°" for r in ROTATIONS])
        rot.set_tooltip_text("Rotate clockwise")
        rot.set_selected(ROTATIONS.index(self.rotate)
                         if self.rotate in ROTATIONS else 0)
        rot.connect("notify::selected", self._on_rotate)
        header.pack_end(rot)

        fit = Gtk.DropDown.new_from_strings([f.capitalize() for f in FITS])
        fit.set_tooltip_text("How to fit images that aren't the panel's size")
        fit.set_selected(FITS.index(self.fit) if self.fit in FITS else 0)
        fit.connect("notify::selected", self._on_fit)
        header.pack_end(fit)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(box)

        self.status = Gtk.Label(label="Connecting to panel…", xalign=0,
                                margin_start=12, margin_end=12,
                                margin_top=8, margin_bottom=8,
                                ellipsize=Pango.EllipsizeMode.END)
        box.append(self.status)
        box.append(Gtk.Separator())

        self.stack = Gtk.Stack(vexpand=True)
        box.append(self.stack)

        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.SINGLE,
                                homogeneous=False, valign=Gtk.Align.START,
                                max_children_per_line=30,
                                row_spacing=12, column_spacing=12,
                                margin_start=12, margin_end=12,
                                margin_top=12, margin_bottom=12)
        self.flow.connect("child-activated", self._on_pick)
        scroller = Gtk.ScrolledWindow(child=self.flow)
        self.stack.add_named(scroller, "grid")

        self.empty = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.stack.add_named(self.empty, "empty")
        self.empty.set_label("Waiting for the panel…")
        self.stack.set_visible_child_name("empty")

    # ----------------------------------------------------------- panel ----
    def _on_connect(self, info):
        first = self.info is None or (info["width"], info["height"]) != \
            (self.info["width"], self.info["height"])
        self.info = info
        if first:
            self.folder = core.image_dir(info["width"], info["height"],
                                         self.image_root)
            self._watch_folder()
            self.refresh()
            if self.current is None:
                last = core.load_state().get("last", {}).get(self.key)
                if last and (self.folder / last).is_file():
                    self.show_image(self.folder / last)
        self._update_status()
        return False

    def _on_error(self, msg):
        self.status.set_label(f"⚠ {msg}")
        return False

    @property
    def key(self):
        return core.geometry_key(self.info["width"], self.info["height"])

    def _update_status(self):
        if not self.info:
            return
        i = self.info
        text = f"{i['model']} · {i['width']}×{i['height']} · {self.folder}"
        if self.paused:
            text += " · paused"
        elif self.current:
            text += f" · showing {self.current.name}"
        self.status.set_label(text)

    # ------------------------------------------------------------ images --
    def _watch_folder(self):
        if self._monitor:
            self._monitor.cancel()
        self._monitor = Gio.File.new_for_path(str(self.folder)) \
            .monitor_directory(Gio.FileMonitorFlags.NONE, None)
        self._monitor.connect("changed", self._on_folder_changed)

    def _on_folder_changed(self, *_):
        if not self._refresh_pending:
            self._refresh_pending = True
            GLib.timeout_add(600, self._deferred_refresh)

    def _deferred_refresh(self):
        self._refresh_pending = False
        self.refresh()
        return False

    def refresh(self):
        if not self.folder:
            return
        while (child := self.flow.get_first_child()) is not None:
            self.flow.remove(child)
        images = core.list_images(self.folder)
        if not images:
            self.empty.set_label(
                f"No images yet.\n\nPut pictures in\n{self.folder}\n"
                "or use the + button.\n\n"
                f"Best results: exactly {self.info['width']}×"
                f"{self.info['height']} pixels.")
            self.stack.set_visible_child_name("empty")
            return
        self.stack.set_visible_child_name("grid")
        tw = max(60, round(THUMB_H * self.info["width"] / self.info["height"]))
        for path in images:
            cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            pic = Gtk.Picture(can_shrink=True,
                              content_fit=Gtk.ContentFit.CONTAIN)
            pic.set_size_request(tw, THUMB_H)
            try:
                pic.set_paintable(make_texture(path, tw, THUMB_H))
            except Exception:
                pic = Gtk.Image(icon_name="image-missing-symbolic",
                                pixel_size=48)
                pic.set_size_request(tw, THUMB_H)
            cell.append(pic)
            label = Gtk.Label(label=path.name,
                              ellipsize=Pango.EllipsizeMode.MIDDLE,
                              max_width_chars=14, tooltip_text=path.name)
            cell.append(label)
            child = Gtk.FlowBoxChild(child=cell)
            child.path = path
            self.flow.append(child)
            if self.current and path == self.current:
                self.flow.select_child(child)

    def _on_pick(self, _flow, child):
        self.show_image(child.path)

    def show_image(self, path):
        if not self.info:
            return
        self.current = Path(path)
        self._render_token += 1
        token = self._render_token
        w, h = self.info["width"], self.info["height"]
        fit, rotate = self.fit, self.rotate
        self.status.set_label(f"Rendering {self.current.name}…")

        def work():
            try:
                jpeg = core.render_jpeg(self.current, w, h, rotate=rotate,
                                        fit=fit)
            except Exception as e:
                GLib.idle_add(self._on_error, f"Cannot load {path}: {e}")
                return
            GLib.idle_add(self._send, token, jpeg)

        threading.Thread(target=work, daemon=True).start()
        core.save_state(last={self.key: self.current.name})

    def _send(self, token, jpeg):
        if token != self._render_token:
            return False  # a newer pick superseded this one
        if self.paused:
            self.pause_btn.set_active(False)  # picking an image resumes
        self.streamer.set_frame(jpeg)
        self._update_status()
        return False

    # ---------------------------------------------------------- controls --
    def _on_pause(self, btn):
        self.paused = btn.get_active()
        if self.paused:
            self.streamer.set_frame(None)
        elif self.current:
            self.show_image(self.current)
        self._update_status()

    def _on_fit(self, dd, _pspec):
        self.fit = FITS[dd.get_selected()]
        core.save_state(fit=self.fit)
        if self.current:
            self.show_image(self.current)

    def _on_rotate(self, dd, _pspec):
        self.rotate = ROTATIONS[dd.get_selected()]
        core.save_state(rotate=self.rotate)
        if self.current:
            self.show_image(self.current)

    def _on_open_folder(self, *_):
        folder = self.folder or self.image_root
        folder.mkdir(parents=True, exist_ok=True)
        Gio.AppInfo.launch_default_for_uri(folder.as_uri(), None)

    def _on_add(self, *_):
        if not self.folder:
            self._on_error("Panel not connected yet — no target folder.")
            return
        flt = Gtk.FileFilter(name="Images")
        for ext in core.IMAGE_EXTS:
            flt.add_suffix(ext.lstrip("."))
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(flt)
        dialog = Gtk.FileDialog(title="Add images", filters=filters)
        dialog.open_multiple(self, None, self._on_add_done)

    def _on_add_done(self, dialog, result):
        try:
            files = dialog.open_multiple_finish(result)
        except GLib.Error:
            return  # cancelled
        last = None
        for i in range(files.get_n_items()):
            src = Path(files.get_item(i).get_path())
            dst = self.folder / src.name
            if src.resolve() != dst.resolve():
                shutil.copy2(src, dst)
            last = dst
        self.refresh()
        if last:
            self.show_image(last)

    def _on_close(self, *_):
        if self._monitor:
            self._monitor.cancel()
        self.streamer.stop()
        return False


class App(Gtk.Application):
    def __init__(self, port, image_root):
        super().__init__(application_id="io.github.vmaxlcd.Picker",
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.port = port
        self.image_root = image_root

    def do_activate(self):
        win = self.get_active_window() or Window(self, self.port,
                                                 self.image_root)
        win.present()


def _on_sigint(callback):
    """Quit cleanly on Ctrl+C. GLibUnix exists on GLib >= 2.80; older
    PyGObject only has the (now deprecated) GLib.unix_signal_add."""
    def handler():
        callback()
        return GLib.SOURCE_REMOVE
    try:
        gi.require_version("GLibUnix", "2.0")
        from gi.repository import GLibUnix
        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, handler)
    except (ImportError, ValueError):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, handler)


def run_ui(port=None, image_root=core.IMAGE_ROOT):
    app = App(port, image_root)
    _on_sigint(app.quit)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(run_ui())
