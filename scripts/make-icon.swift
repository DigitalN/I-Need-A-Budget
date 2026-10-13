#!/usr/bin/env swift
// Draws the app icon (a green envelope on a rounded square) as an .iconset folder.
// scripts/make-icon.sh turns it into Application Files/INeedABudget.icns.
//
//   swift scripts/make-icon.swift <output.iconset>

import AppKit

let output = URL(fileURLWithPath: CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "INeedABudget.iconset")
try? FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)

func color(_ hex: UInt32, _ alpha: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat((hex >> 16) & 0xff) / 255, green: CGFloat((hex >> 8) & 0xff) / 255,
            blue: CGFloat(hex & 0xff) / 255, alpha: alpha)
}

func draw(_ pixels: Int) -> Data {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels, bitsPerSample: 8,
                               samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                               bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let s = CGFloat(pixels) / 1024
    // macOS icon grid: an 824 pt rounded square centred in 1024.
    let tile = NSRect(x: 100 * s, y: 100 * s, width: 824 * s, height: 824 * s)
    let shadow = NSShadow()
    shadow.shadowColor = NSColor(white: 0, alpha: 0.28)
    shadow.shadowBlurRadius = 18 * s
    shadow.shadowOffset = NSSize(width: 0, height: -8 * s)
    NSGraphicsContext.saveGraphicsState()
    shadow.set()
    let square = NSBezierPath(roundedRect: tile, xRadius: 185 * s, yRadius: 185 * s)
    NSGradient(starting: color(0x34c06e), ending: color(0x168a49))!.draw(in: square, angle: -90)
    NSGraphicsContext.restoreGraphicsState()

    // The envelope.
    let env = NSRect(x: 232 * s, y: 312 * s, width: 560 * s, height: 400 * s)
    let body = NSBezierPath(roundedRect: env, xRadius: 54 * s, yRadius: 54 * s)
    color(0xffffff).setFill()
    body.fill()
    let flap = NSBezierPath()
    flap.move(to: NSPoint(x: env.minX + 40 * s, y: env.maxY - 44 * s))
    flap.line(to: NSPoint(x: env.midX, y: env.midY - 10 * s))
    flap.line(to: NSPoint(x: env.maxX - 40 * s, y: env.maxY - 44 * s))
    flap.lineWidth = 34 * s
    flap.lineCapStyle = .round
    flap.lineJoinStyle = .round
    color(0x1f9d55).setStroke()
    flap.stroke()
    // A coin peeking out, so it reads as money.
    let coin = NSRect(x: 612 * s, y: 222 * s, width: 210 * s, height: 210 * s)
    color(0xffd24a).setFill()
    NSBezierPath(ovalIn: coin).fill()
    color(0xe0a800).setStroke()
    let ring = NSBezierPath(ovalIn: coin.insetBy(dx: 22 * s, dy: 22 * s))
    ring.lineWidth = 12 * s
    ring.stroke()
    let dollar = NSAttributedString(string: "$", attributes: [
        .font: NSFont.systemFont(ofSize: 132 * s, weight: .heavy), .foregroundColor: color(0xa86f00)])
    let ds = dollar.size()
    dollar.draw(at: NSPoint(x: coin.midX - ds.width / 2, y: coin.midY - ds.height / 2))
    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

for (name, pixels) in [("16x16", 16), ("16x16@2x", 32), ("32x32", 32), ("32x32@2x", 64), ("128x128", 128),
                       ("128x128@2x", 256), ("256x256", 256), ("256x256@2x", 512), ("512x512", 512),
                       ("512x512@2x", 1024)] {
    try! draw(pixels).write(to: output.appendingPathComponent("icon_\(name).png"))
}
