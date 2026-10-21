#!/usr/bin/env python3
"""README screenshots of the dev preview, in light and dark, with WebKit (as the app's window).

    python3 scripts/demo_data.py
    (start the `demo` entry in .claude/launch.json: the preview on that folder, port 8766)
    python3 scripts/screenshots.py [--url http://127.0.0.1:8766] [--out docs/screenshots]

Draws the Budget page off screen at 1280×820, then a close-up of one part of a page for
each feature the README shows (in a narrower window, so its text stays readable), all
saved at 2×, and quits.

Only ever point it at a preview of made-up data (scripts/demo_data.py): the pictures go
into the public repository.
"""

import argparse
import json
from pathlib import Path

import AppKit
import objc
import WebKit
from Foundation import NSURL, NSURLRequest
from PyObjCTools import AppHelper

ROOT = Path(__file__).resolve().parent.parent
WINDOW = (1280, 820)
PAD = 6  # page background kept around a close-up (less than the gap between cards)
# (file name, page, what a close-up frames: a CSS selector, or None for the whole window;
#  window width: narrower than WINDOW keeps a close-up's text readable at README size)
SHOTS = [
    ("budget", "budget", None, WINDOW[0]),
    ("inbox", "inbox", ".items > :nth-child(-n+2)", 1000),
    ("ready-to-assign", "budget", ".card.hero", 1000),
    ("subscriptions", "subscriptions", ".stack > .card", 1200),
    ("charts", "dashboard", ".charts", 1000),
]


class Shooter(AppKit.NSObject):
    def initWithURL_out_(self, url, out):
        self = self.init()
        self.out = Path(out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.jobs = [(shot, theme) for theme in ("light", "dark") for shot in SHOTS]
        frame = AppKit.NSMakeRect(0, 0, *WINDOW)
        self.window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, AppKit.NSWindowStyleMaskBorderless, AppKit.NSBackingStoreBuffered, False)
        self.window.setFrameOrigin_((-10000, -10000))  # off screen
        self.view = WebKit.WKWebView.alloc().initWithFrame_configuration_(frame, WebKit.WKWebViewConfiguration.new())
        self.view.setNavigationDelegate_(self)
        self.window.setContentView_(self.view)
        self.window.orderFront_(None)
        self.view.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))
        return self

    def webView_didFinishNavigation_(self, view, navigation):
        self.after(1.5, self.next)

    @objc.python_method
    def after(self, seconds, fn):
        AppKit.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(seconds, False, lambda _t: fn())

    @objc.python_method
    def next(self):
        if not self.jobs:
            AppHelper.stopEventLoop()
            return
        (_name, page, selector, width), theme = self.jobs[0]
        self.window.setContentSize_((width, 1000) if selector else WINDOW)
        appearance = AppKit.NSAppearance.appearanceNamed_(
            AppKit.NSAppearanceNameDarkAqua if theme == "dark" else AppKit.NSAppearanceNameAqua)
        self.view.setAppearance_(appearance)
        js = (f"document.documentElement.setAttribute('data-theme', '{theme}'); navigate('{page}'); "
              "window.scrollTo(0, 0);")
        self.view.evaluateJavaScript_completionHandler_(js, lambda *_: self.after(1.2, self.measure))

    @objc.python_method
    def measure(self):
        selector = self.jobs[0][0][2]
        if not selector:
            self.shoot(None)
            return
        # The box around everything the selector matches, plus a little of the page around it
        js = (f"(() => {{ const r = [...document.querySelectorAll({json.dumps(selector)})]"
              ".map(e => e.getBoundingClientRect()); if (!r.length) return '';"
              f" const x = Math.min(...r.map(b => b.left)) - {PAD}, y = Math.min(...r.map(b => b.top)) - {PAD};"
              f" return JSON.stringify([x, y, Math.max(...r.map(b => b.right)) + {PAD} - x,"
              f" Math.max(...r.map(b => b.bottom)) + {PAD} - y]); }})()")
        self.view.evaluateJavaScript_completionHandler_(js, lambda box, _error: self.shoot(box))

    @objc.python_method
    def shoot(self, box):
        config = WebKit.WKSnapshotConfiguration.new()
        if box:
            config.setRect_(AppKit.NSMakeRect(*json.loads(box)))
        elif self.jobs[0][0][2]:
            print("Nothing matches", self.jobs[0][0][2], flush=True)
        self.view.takeSnapshotWithConfiguration_completionHandler_(config, self.saved)

    @objc.python_method
    def saved(self, image, error):
        (name, _page, _selector, _width), theme = self.jobs.pop(0)
        if image is not None:
            rep = AppKit.NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
            png = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
            path = self.out / f"{name}-{theme}.png"
            png.writeToFile_atomically_(str(path), True)
            print("Saved", path.relative_to(ROOT) if path.is_relative_to(ROOT) else path, flush=True)
        else:
            print("Failed", name, theme, error, flush=True)
        self.next()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8766")
    parser.add_argument("--out", default=str(ROOT / "docs" / "screenshots"))
    args = parser.parse_args()
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyProhibited)
    shooter = Shooter.alloc().initWithURL_out_(args.url, args.out)  # noqa: F841 (kept alive)
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
