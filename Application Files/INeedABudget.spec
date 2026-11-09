# SPDX-License-Identifier: GPL-3.0-only
# PyInstaller spec for I Need A Budget.
# A normal windowed app (Dock icon, quits when its window closes).
# Data lives in ~/Library/Application Support/I Need A Budget, logs in ~/Library/Logs/I Need A Budget.

import os
import re
import sys

REPO = os.path.dirname(SPECPATH)
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import third_party_licenses  # noqa: E402

ICON_PATH = os.path.join(SPECPATH, 'INeedABudget.icns')
# The version lives in main.py only.
with open(os.path.join(SPECPATH, 'main.py'), encoding='utf-8') as f:
    VERSION = re.search(r'^VERSION = "([^"]+)"', f.read(), re.MULTILINE).group(1)

a = Analysis(
    ['main.py'],
    pathex=[SPECPATH],
    binaries=[],
    datas=[
        ('index.html', '.'),
    ],
    hiddenimports=[
        'webview',
        'webview.platforms.cocoa',
        'webview.dom',
        'objc',
        'Foundation',
        'AppKit',
        'WebKit',
        'PyObjCTools.AppHelper',
        'rapidfuzz',
        'rapidfuzz.fuzz',
    ],
    hookspath=[],
    runtime_hooks=[],
    # Other pywebview backends and unused stdlib GUI toolkits.
    excludes=[
        'tkinter',
        'webview.platforms.android',
        'webview.platforms.cef',
        'webview.platforms.edgechromium',
        'webview.platforms.gtk',
        'webview.platforms.mshtml',
        'webview.platforms.qt',
        'webview.platforms.winforms',
        'pytest',
    ],
    noarchive=False,
)

# The licenses, in Contents/Resources as .txt so they open in TextEdit. The third-party
# file lists every package whose modules were collected; the build stops if one has no license.
bundled = {name.split('/')[0].split('.')[0] for name, _, _ in a.pure + a.binaries + a.datas}
third_party = os.path.join(workpath, 'Third-party licenses.txt')
with open(third_party, 'w', encoding='utf-8') as f:
    f.write(third_party_licenses.for_modules(bundled))
a.datas += [
    ('LICENSE.txt', os.path.join(REPO, 'LICENSE'), 'DATA'),
    ('NOTICE.txt', os.path.join(REPO, 'docs', 'NOTICE'), 'DATA'),
    ('Third-party licenses.txt', third_party, 'DATA'),
]

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='I Need A Budget',
    debug=False,
    strip=False,
    upx=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='I Need A Budget',
)

app = BUNDLE(
    coll,
    name='I Need A Budget.app',
    icon=ICON_PATH if os.path.exists(ICON_PATH) else None,
    bundle_identifier='com.digitaln.ineedabudget',
    info_plist={
        'CFBundleName': 'I Need A Budget',
        'CFBundleDisplayName': 'I Need A Budget',
        'CFBundleVersion': VERSION,
        'CFBundleShortVersionString': VERSION,
        'LSMinimumSystemVersion': '12.0',
        'LSApplicationCategoryType': 'public.app-category.finance',
        'NSHighResolutionCapable': True,
        'NSDownloadsFolderUsageDescription':
            'I Need A Budget imports the bank files you download, then moves them into its own folder.',
        'NSDocumentsFolderUsageDescription':
            'I Need A Budget saves a backup to your Documents folder, so it needs access for that.',
        'NSHumanReadableCopyright': '© 2026 DigitalN. Free software under the GNU GPL v3.',
    },
)
