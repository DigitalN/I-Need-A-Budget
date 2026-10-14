#!/usr/bin/env python3
"""README screenshots of the dev preview, in light and dark, with WebKit (as the app's window).

    python3 scripts/demo_data.py
    (start the `demo` entry in .claude/launch.json: the preview on that folder, port 8766)
    python3 scripts/screenshots.py [--url http://127.0.0.1:8766] [--out docs/screenshots]

Draws each page off screen at 1280×820 (saved at 2×) and quits.

Only ever point it at a preview of made-up data (scripts/demo_data.py): the pictures go
into the public repository.
"""

import argparse
from pathlib import Path

import AppKit
import objc
import WebKit
from Foundation import NSURL, NSURLRequest
from PyObjCTools import AppHelper

ROOT = Path(__file__).resolve().parent.parent
WIDTH, HEIGHT = 1280, 820
PAGES = ["dashboard", "budget", "inbox", "subscriptions", "accounts"]


class Shooter(AppKit.NSObject):
    def initWithURL_out_(self, url, out):
        self = self.init()
        self.out = Path(out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.jobs = [(page, theme) for theme in ("light", "dark") for page in PAGES]
        frame = AppKit.NSMakeRect(0, 0, WIDTH, HEIGHT)
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
        page, theme = self.jobs[0]
        appearance = AppKit.NSAppearance.appearanceNamed_(
            AppKit.NSAppearanceNameDarkAqua if theme == "dark" else AppKit.NSAppearanceNameAqua)
        self.view.setAppearance_(appearance)
        js = (f"document.documentElement.setAttribute('data-theme', '{theme}'); navigate('{page}'); "
              "window.scrollTo(0, 0);")
        self.view.evaluateJavaScript_completionHandler_(js, lambda *_: self.after(1.2, self.shoot))

    @objc.python_method
    def shoot(self):
        config = WebKit.WKSnapshotConfiguration.new()
        self.view.takeSnapshotWithConfiguration_completionHandler_(config, self.saved)

    @objc.python_method
    def saved(self, image, error):
        page, theme = self.jobs.pop(0)
        if image is not None:
            rep = AppKit.NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
            png = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
            path = self.out / f"{page}-{theme}.png"
            png.writeToFile_atomically_(str(path), True)
            print("Saved", path.relative_to(ROOT) if path.is_relative_to(ROOT) else path, flush=True)
        else:
            print("Failed", page, theme, error, flush=True)
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
