# SPDX-License-Identifier: GPL-3.0-only
# PyInstaller spec for I Need A Budget, on a Mac or a Windows PC.
# A normal windowed app (Dock icon or taskbar button, quits when its window closes).
# Mac: "I Need A Budget.app" (scripts/build.sh). Windows: the folder "I Need A Budget", with
# "I Need A Budget.exe" in it, which scripts/installer.iss installs (scripts/build-windows.sh).
# Where each keeps its data is computer_mac.py's and computer_windows.py's.

import os
import re
import sys

REPO = os.path.dirname(SPECPATH)
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import third_party_licenses  # noqa: E402

MAC = sys.platform == 'darwin'
COPYRIGHT = '© 2026 DigitalN. Free software under the GNU GPL v3.'
# The version lives in main.py only.
with open(os.path.join(SPECPATH, 'main.py'), encoding='utf-8') as f:
    VERSION = re.search(r'^VERSION = "([^"]+)"', f.read(), re.MULTILINE).group(1)

# Each system's side of pywebview and of computer.py, and nothing of the other's.
if MAC:
    ICON_PATH = os.path.join(SPECPATH, 'INeedABudget.icns')
    SYSTEMS = ['webview.platforms.cocoa', 'objc', 'Foundation', 'AppKit', 'WebKit', 'PyObjCTools.AppHelper',
               'computer_mac']
    OTHERS = ['webview.platforms.edgechromium', 'webview.platforms.winforms', 'computer_windows']
else:
    ICON_PATH = os.path.join(SPECPATH, 'INeedABudget.ico')
    SYSTEMS = ['webview.platforms.winforms', 'webview.platforms.edgechromium', 'clr', 'computer_windows']
    OTHERS = ['webview.platforms.cocoa', 'computer_mac']

datas = [('index.html', '.')]
# Windows updates are checked with our release key's public half (ADR 0009, #188).
RELEASE_KEY = os.path.join(SPECPATH, 'release-key.pub')
if not MAC and os.path.exists(RELEASE_KEY):
    datas.append((RELEASE_KEY, '.'))

a = Analysis(
    ['main.py'],
    pathex=[SPECPATH],
    binaries=[],
    datas=datas,
    hiddenimports=['webview', 'webview.dom', 'rapidfuzz', 'rapidfuzz.fuzz', *SYSTEMS],
    hookspath=[],
    runtime_hooks=[],
    # Other pywebview backends and unused stdlib GUI toolkits.
    excludes=[
        'tkinter',
        'webview.platforms.android',
        'webview.platforms.cef',
        'webview.platforms.gtk',
        'webview.platforms.mshtml',
        'webview.platforms.qt',
        'pytest',
        *OTHERS,
    ],
    noarchive=False,
)

# The licenses, as .txt so they open in TextEdit or Notepad: in Contents/Resources on a Mac, in
# the _internal folder on Windows (the installer puts them beside the app too). The third-party
# file lists every package whose modules were collected; the build stops if one has no license.
bundled = {name.split('/')[0].split('\\')[0].split('.')[0] for name, _, _ in a.pure + a.binaries + a.datas}
third_party = os.path.join(workpath, 'Third-party licenses.txt')
with open(third_party, 'w', encoding='utf-8') as f:
    f.write(third_party_licenses.for_modules(bundled))
a.datas += [
    ('LICENSE.txt', os.path.join(REPO, 'LICENSE'), 'DATA'),
    ('NOTICE.txt', os.path.join(REPO, 'docs', 'NOTICE'), 'DATA'),
    ('Third-party licenses.txt', third_party, 'DATA'),
]

pyz = PYZ(a.pure, a.zipped_data)


def windows_version():
    """What Windows shows in the .exe's Properties → Details."""
    from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                     VarFileInfo, VarStruct, VSVersionInfo)

    numbers = tuple(int(n) for n in re.findall(r'\d+', VERSION)[:3]) + (0,)
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=numbers, prodvers=numbers),
        kids=[
            StringFileInfo([StringTable('040904B0', [
                StringStruct('CompanyName', 'DigitalN'),
                StringStruct('FileDescription', 'I Need A Budget'),
                StringStruct('FileVersion', VERSION),
                StringStruct('InternalName', 'I Need A Budget'),
                StringStruct('LegalCopyright', COPYRIGHT),
                StringStruct('OriginalFilename', 'I Need A Budget.exe'),
                StringStruct('ProductName', 'I Need A Budget'),
                StringStruct('ProductVersion', VERSION),
            ])]),
            VarFileInfo([VarStruct('Translation', [0x0409, 1200])]),
        ])


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
    **({} if MAC else {'icon': ICON_PATH, 'version': windows_version()}),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='I Need A Budget',
)

if MAC:
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
            'NSHumanReadableCopyright': COPYRIGHT,
        },
    )
