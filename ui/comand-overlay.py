#!/usr/bin/env python3
"""Our UI as a click-through layer above Android.

cog cannot do this. A floating window on top receives every touch over its
area, so with cog as the overlay the panel's touchscreen reached the overlay
and never Sygic -- fine only if the panel had no touch, which it does. This is
a layer-shell surface instead, and layer-shell surfaces can declare an input
region: the part of the surface that accepts input. Set it empty and touches
pass straight through to whatever is underneath.

The region is not fixed, because what we want changes with what is on screen:

    nothing showing   empty region     every touch goes to Android
    volume popup      empty region     it is a readout, not a control
    menu open         full region      the menu is being used, so it takes input

The menu is the only thing here that is interactive, so it is the only thing
that takes the touchscreen away from Sygic, and only while it is open.

State comes from comand-bridge.py's event stream rather than from the page, so
the page stays a page and the decision lives in one place.

Being layer-shell also fixes something that bit the floating version: a
fullscreen window is drawn above floating ones in sway, so fullscreen Android
hid the overlay completely. The overlay layer is above fullscreen windows by
definition, so Android can be fullscreen again.

Usage:
    comand-overlay.py                    # http://127.0.0.1:8080
    comand-overlay.py --url http://...
"""

from __future__ import annotations

import argparse
import json
import threading
import urllib.request

# gtk4-layer-shell has to be loaded before GTK touches libwayland, or its hooks
# are never installed and every call fails with "GtkWindow is not a layer
# surface" -- which reads like the init call was forgotten when in fact it ran.
# The library's own documentation calls for LD_PRELOAD with language bindings.
# Re-exec once with it set rather than making the caller remember.
import os
import sys

_LAYER_LIB = "libgtk4-layer-shell.so.0"
# GTK's GL renderer alternated a whole frame with one holding only the changed
# patches: the panel flashed black on knob moves, and at 60 Hz while a long radio
# title scrolled. Disabling WebKit's damage tracking or its GPU use did not help.
os.environ.setdefault("GSK_RENDERER", "cairo")
# The session sets this for cog on the Pi. On the L560 its fallback buffers are
# what came back holding only the changed parts of the page: black flashes on
# every change. DMA-BUF: none in the same test.
os.environ.pop("WEBKIT_DISABLE_DMABUF_RENDERER", None)

if not os.environ.get("COMAND_OVERLAY_PRELOADED"):
    os.environ["COMAND_OVERLAY_PRELOADED"] = "1"
    pre = os.environ.get("LD_PRELOAD", "")
    os.environ["LD_PRELOAD"] = (_LAYER_LIB + (":" + pre if pre else ""))
    os.execv(sys.executable, [sys.executable] + sys.argv)

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
gi.require_version("WebKit", "6.0")

import cairo  # noqa: E402  -- python3-cairo; needed for the input region
from gi.repository import Gdk, Gsk, Gtk, Gtk4LayerShell as LayerShell, GLib, WebKit  # noqa: E402

DEFAULT_URL = "http://127.0.0.1:8080/?kiosk=1&overlay=1"
EVENTS = "http://127.0.0.1:8080/events"


class Overlay:
    def __init__(self, url: str):
        self.app = Gtk.Application(application_id="org.comand.Overlay")
        self.app.connect("activate", self.on_activate)
        self.url = url
        self.window: Gtk.Window | None = None
        self.interactive = False
        self.asleep = False
        self.ui_seq = None
        self.view = None
        self.fixed = None
        self.size = None

    # ------------------------------------------------------------- window --
    def on_activate(self, app):
        win = Gtk.ApplicationWindow(application=app)
        self.window = win

        LayerShell.init_for_window(win)
        # The overlay layer sits above normal and fullscreen windows alike.
        LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
        LayerShell.set_namespace(win, "comand-overlay")
        for edge in (LayerShell.Edge.TOP, LayerShell.Edge.BOTTOM,
                     LayerShell.Edge.LEFT, LayerShell.Edge.RIGHT):
            LayerShell.set_anchor(win, edge, True)
        # -1 means "do not reserve any space and do not let anything reserve
        # space against me": the surface covers the whole output and Android
        # keeps the full screen underneath.
        LayerShell.set_exclusive_zone(win, -1)
        # NONE, not ON_DEMAND: the knob is the input device, and a surface that
        # can take keyboard focus would steal it from Android.
        LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.NONE)

        # The WebView being transparent is not enough: the window and its
        # default .background style class paint first, and an opaque GTK
        # background covers Android just as effectively as an opaque page.
        css = Gtk.CssProvider()
        css.load_from_string(
            "window, window.background { background: none; background-color: transparent; }")
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        view = WebKit.WebView()
        # Transparent, or the page's own background would paint over Android.
        transparent = Gdk.RGBA()
        transparent.parse("rgba(0,0,0,0)")
        view.set_background_color(transparent)
        # Nothing smaller than 14 px. COMAND's screen has 230 lines and is read
        # from the driver's seat; the page has labels at 11-12 px that come out
        # as mush there. WebKit grows them; the layout already allows for it.
        view.get_settings().set_minimum_font_size(14)
        # The page's console to our stdout: errors in a state update are
        # otherwise invisible, and look like a screen that stopped drawing.
        view.get_settings().set_enable_write_console_messages_to_stdout(True)
        # Whole frames, not damaged regions. With WebKit's damage tracking on,
        # each frame it handed over held only what it believed had changed, and
        # the panel showed those patches -- or nothing -- over an empty screen:
        # the "black screen" after knob turns, on the keypad, on the battery
        # page. WebKit's own snapshot of the page was correct all along.
        settings = view.get_settings()
        features = WebKit.Settings.get_all_features()
        for i in range(features.get_length()):
            f = features.get(i)
            if f.get_identifier() in ("PropagateDamagingInformation", "UseDamagingInformationForCompositing",
                                      "UnifyDamagedRegions"):
                settings.set_feature_enabled(f, False)
        # The page comes from the bridge, which may not be serving yet (boot,
        # or the bridge restarting): a failed load is retried rather than left
        # as an empty, transparent page -- a blank panel with nothing wrong.
        def failed(v, event, uri, err):
            print(f"load failed ({err.message}); retrying", flush=True)
            GLib.timeout_add(2000, lambda: v.load_uri(self.url) or False)
            return True
        view.connect("load-failed", failed)
        view.load_uri(self.url)
        # COMAND shows the output's pixels half as wide as they are tall, so the
        # page is laid out at half the width and stretched here. Stretched in
        # CSS (scaleX on the body), WebKit showed only the changed parts of the
        # page for a moment after every change: black flashes on the panel.
        self.fixed = Gtk.Fixed()
        self.fixed.put(view, 0, 0)
        win.set_child(self.fixed)
        self.view = view

        # The region is in surface coordinates, so it has to be redrawn when
        # the surface is: an output that comes up at one size and settles at
        # another would otherwise leave a region covering the wrong area.
        def on_realize(*_):
            surface = win.get_surface()
            if surface is not None:
                surface.connect("layout", lambda *_: self.on_layout())
            GLib.idle_add(self.apply_region)
        win.connect("realize", on_realize)
        win.present()

        threading.Thread(target=self.watch_state, daemon=True).start()

    def on_layout(self) -> None:
        self.apply_region()
        surface = self.window.get_surface() if self.window else None
        if surface is None:
            return
        w, h = surface.get_width(), surface.get_height()
        if not w or not h or (w, h) == self.size:
            return
        self.size = (w, h)
        wide = w / h > 2.2            # the page's own test for wide pixels, before
        self.view.set_size_request(w // 2 if wide else w, h)
        self.fixed.set_child_transform(
            self.view, Gsk.Transform.new().scale(2.0 if wide else 1.0, 1.0))

    # ------------------------------------------------------- input region --
    def apply_region(self) -> bool:
        """Empty region = click-through; the full rectangle = takes input.

        The documented way to say "all of it" is a NULL region, but PyGObject
        annotates the argument as non-nullable and raises TypeError rather than
        passing NULL through. That threw inside an idle callback, where nothing
        surfaces it, so the region stayed empty for the life of the process and
        no tap ever reached the page. An explicit rectangle covering the
        surface says the same thing and survives the binding.
        """
        if not self.window:
            return False
        surface = self.window.get_surface()
        if surface is None:
            return False
        if self.interactive:
            w = surface.get_width() or 1
            h = surface.get_height() or 1
            surface.set_input_region(
                cairo.Region(cairo.RectangleInt(0, 0, w, h)))
        else:
            surface.set_input_region(cairo.Region())      # empty: pass through
        return False

    def set_interactive(self, want: bool) -> bool:
        if want != self.interactive:
            self.interactive = want
            self.apply_region()
        return False

    # ------------------------------------------------------------ standby --
    def wake(self) -> bool:
        """Start over after the output has been off.

        Standby turns the panel off with DPMS, and a layer surface does not
        survive that on its own: while the output is disabled no frame
        callbacks arrive, GTK stops drawing waiting for one, and when the
        output comes back the callback it was waiting for never does. The
        result is a compositor showing an output that works -- Android renders
        on its own workspace -- and our surface never committing another frame
        again. A black panel with the page behind it alive, connected and
        updating, which is exactly what it looks like when the page has
        broken, and is why this cost two rounds of chasing the wrong thing.

        Two fixes were tried before this one. Hiding and re-showing the
        window recreates the surface and worked most of the time, but raced
        the output coming back and left the panel black often enough to be
        worse than useless. Re-execing the whole process always worked and
        took over thirty seconds to reload the page, which is not a thing to
        do to somebody who has just woken their car.

        So: wait for the output to actually be ready, then recreate the
        surface, then kick the frame clock by hand. The kick is the part that
        matters -- what stalls is GTK waiting on a frame callback, and asking
        the clock for a paint phase is telling it to stop waiting.
        """
        if not self.window:
            return False
        self.window.set_visible(False)
        self.window.set_visible(True)

        # Twice: once as soon as the surface exists, once after it has had a
        # chance to map. A second kick on a healthy surface costs one frame.
        GLib.timeout_add(150, self.kick)
        GLib.timeout_add(1000, self.kick)
        return False

    def kick(self) -> bool:
        """Ask GTK for a paint now, in case it is stuck waiting on a frame callback."""
        surface = self.window.get_surface() if self.window else None
        if surface is None:
            return False
        self.apply_region()
        clock = surface.get_frame_clock()
        if clock is not None:
            clock.request_phase(Gdk.FrameClockPhase.PAINT)
        if self.window:
            self.window.queue_draw()
        return False

    # ------------------------------------------------------------- events --
    def watch_state(self) -> None:
        """Follow the bridge's stream; reconnect for as long as we are alive."""
        while True:
            try:
                with urllib.request.urlopen(EVENTS, timeout=30) as r:
                    for raw in r:
                        line = raw.decode("utf-8", "replace").strip()
                        if not line.startswith("data:"):
                            continue
                        try:
                            st = json.loads(line[5:].strip())
                        except ValueError:
                            continue
                        # Interactive whenever our own screen is the thing on
                        # display -- the home menu and the radio grid alike --
                        # not only when the knob has raised a menu. The radio
                        # tiles are meant to be pressed, and an empty input
                        # region would have sent every tap through to whatever
                        # is behind.
                        want = bool(st.get("menuOpen")) or st.get("screen") == "menu"
                        GLib.idle_add(self.set_interactive, want)

                        # A knob key is a moment the screen must change. The
                        # panel went black under the knob once with the page
                        # alive behind it -- the same stalled frame clock as
                        # after standby -- so every key kicks it, twice.
                        seq = (st.get("ui") or {}).get("seq")
                        if seq != self.ui_seq:
                            self.ui_seq = seq
                            GLib.idle_add(self.kick)
                            GLib.timeout_add(400, self.kick)

                        # Coming back from standby, not going into it: the
                        # surface is fine until the output is disabled under
                        # it, and what needs doing is on the way out.
                        asleep = bool(st.get("standby"))
                        if self.asleep and not asleep:
                            # A moment after, not immediately: the bridge
                            # publishes this as soon as swaymsg returns, and
                            # the output takes a little longer than that to be
                            # ready for a new surface.
                            GLib.timeout_add(1200, self.wake)
                        self.asleep = asleep
            except Exception:
                pass
            # The bridge may simply not be up yet; this is a car, not a server.
            threading.Event().wait(2)

    def run(self) -> int:
        return self.app.run(None)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=DEFAULT_URL)
    args = ap.parse_args()
    return Overlay(args.url).run()


if __name__ == "__main__":
    sys.exit(main())
