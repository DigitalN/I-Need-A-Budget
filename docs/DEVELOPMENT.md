# Developing I Need A Budget

Notes for building from source and publishing releases. For using it, see the
[README](../README.md). The architecture and the rules the code follows are in
[CLAUDE.md](../.claude/CLAUDE.md).

## Build from source

You need Python 3 from [python.org](https://www.python.org/downloads/) and the pinned
packages in `Application Files/requirements.txt` (`scripts/First Time Setup.command` installs
them and switches on the privacy hook). Then:

```bash
scripts/install.sh --open
```

This builds `I Need A Budget.app`, signs it with your **Apple Development** certificate,
installs it to `/Applications` and opens it.

Without a certificate the build is signed ad hoc: it works, but it can't update itself.
A free Apple ID is enough to get one: Xcode → Settings → Apple Accounts → sign in →
select your **Personal Team** → Manage Certificates… → **+** → **Apple Development**.

Builds go to `~/Library/Caches/I Need A Budget-build`, not the repository: folders synced
by iCloud Drive (such as `~/Documents`) add Finder metadata to app bundles, and codesign
rejects bundles that have it. Every checkout and worktree shares that folder, so
`build.sh` and `install.sh` take one lock
(`scripts/build-lock.sh`): a second build waits, saying so. PyInstaller's binary cache is
the build's own (`PYINSTALLER_CONFIG_DIR` inside that folder), because another project's
`--clean` build empties the shared one in `~/Library/Application Support/pyinstaller`,
which used to make ours fail half-way ("lipo: can't open input file").

To run from source without building (uses your real data unless you say otherwise):

```bash
cd "Application Files" && INAB_DATA_DIR=/tmp/inab-test python3 main.py
```

`INAB_DATA_DIR` points at a throwaway data folder (its backups go in it too, never into
the real Documents); `INAB_TODAY=YYYY-MM-DD` pins today's
date; `INAB_DEBUG=1` turns on the web inspector. Tests (`scripts/tests/`): `python3 -m pytest`.

### On Windows

Python 3.13, the pinned packages (`python -m pip install -r "Application Files/requirements.txt"`,
in a virtual environment if you like: on Windows they include pythonnet, which pywebview draws its
window with) and, for the installer, [Inno Setup](https://jrsoftware.org/isinfo.php) 6.3 or later. Run from
source as on a Mac (`INAB_DATA_DIR` keeps it off your real data). In Git for Windows' bash:

```bash
scripts/build-windows.sh
```

This builds `I Need A Budget.exe` with PyInstaller and the installer,
`I-Need-A-Budget-<version>-Setup.exe` (`scripts/installer.iss`), in
`%LOCALAPPDATA%\I Need A Budget-build`, outside the repository (which OneDrive may sync).
The installer is per-user and unsigned: no administrator, into
`%LOCALAPPDATA%\Programs\I Need A Budget`. The Windows icon, `Application Files/INeedABudget.ico`,
is made from the Mac's by `scripts/make-ico.py` (it needs Pillow, which the app doesn't).

Python from the Microsoft Store keeps its registry writes in a copy of its own: run from source
with it, the window's size and place (`HKEY_CURRENT_USER\Software\I Need A Budget`) aren't the
ones `reg query` or the installed app see.

### Checks on GitHub

Every pull request into `main` or `claude/cross-platform`, and every push to them, runs the
whole suite on macOS and on Windows (`.github/workflows/tests.yml`): Python 3.13, the pinned
packages (pyobjc's are the Mac's only), node for the page tests, the privacy hooks switched on.
The pull request shows both checks, and its Checks tab has each test's output. Both count: a
failure on either system fails the run. The tests use only made-up data and nothing needs
a secret, so a pull request from someone else's copy runs the same way. GitHub's machines have
no `.git-denylist`, so there the privacy test and the hooks check card numbers, bank files and
databases, not your private words: those are only checked on your own computer. Making the
checks required for merging into `main` is the maintainer's setting (Settings → Branches).

The same run builds both apps, unsigned, to try: Build (Windows) makes the installer and Build
(macOS) the Mac app, signed ad hoc and zipped (it can't update itself). Both are in the run's
Summary, under Artifacts, for 30 days. The Mac's disk image and the signed builds are the
release's.

A pull request or push that changes how a release is built or checked (the workflow, the build,
signing and checking scripts, the spec, the updater: the paths in `.github/workflows/release.yml`)
also runs a dry run of the release, Release in its Checks: everything a release does but publish,
signed with throwaway identities made in the run (*Release checklist*).

### Trying a pull request's Windows installer

On a Windows 10 or 11 PC:

1. Open the pull request's Checks tab → Tests → Summary, and download
   `I-Need-A-Budget-<version>-Setup.exe` under Artifacts.
2. Open it. It's unsigned, so Windows says "Windows protected your PC": More info → Run anyway.
   It shouldn't ask for an administrator. Leave Open I Need A Budget ticked and finish.
3. The app opens on onboarding. Put made-up bank files in Downloads
   (`python scripts/demo_data.py --files-only` writes two Quicken files there), then Look again:
   they're listed. Go through the steps to the Dashboard.
4. Start → I Need A Budget is there. With the app open, open it again from Start: the window
   comes forward, and nothing else opens. Minimize it first and try again.
5. Settings: Light, Dark and System change the title bar too. Each Open button opens File
   Explorer; Release notes your browser; License a read-only copy in Notepad, and again
   on a second click.
6. Drag a bank file from File Explorer onto the window: it's imported.
7. Type an amount in an Assigned box and close the window with ✕ before leaving the box: it's
   saved (it's there when you reopen). Do the same with Alt+F4.
8. Move and resize the window, close it, open it: it's where you left it. Maximize it, close,
   open: maximized.
9. Settings → Apps → Installed apps → I Need A Budget → Uninstall. Then
   `%LOCALAPPDATA%\I Need A Budget` is still there, with the budget. Install again: the
   budget's back, without onboarding.

The app icon is drawn by `scripts/make-icon.swift`. To redraw it:

```bash
swift scripts/make-icon.swift /tmp/INeedABudget.iconset && iconutil -c icns /tmp/INeedABudget.iconset -o "Application Files/INeedABudget.icns"
```

## The dev preview

```bash
python3 scripts/preview.py [--data FOLDER] [--today 2026-10-12] [--platform windows]
```

Serves `index.html` on http://127.0.0.1:8765 with a stand-in for pywebview's bridge, so
the real page drives the real engine (`BudgetService.dispatch`) over localhost. Use it to
look at and click through the interface in a browser (Claude: the `preview` entry in
`.claude/launch.json`, in the browser pane). It's a development tool only, never bundled,
and it refuses to use the app's real data folder. The default data folder is
`~/Library/Caches/I Need A Budget-preview`, and its Downloads folder is `downloads` inside
it (created empty; the preview never uses your real `~/Downloads` and refuses to start if its
`settings.json` points there). Put copies of bank files in it to try onboarding.
`--platform mac` or `--platform windows` pretends to be that system (it's this computer's by
default): the page's wording and keys follow it. A browser draws it with this computer's fonts,
and Chromium (Chrome, Edge, Claude's browser pane) is the engine the Windows app uses.

## README screenshots

`docs/screenshots/` holds the Budget page and a close-up for each feature the README
shows, in light and dark, taken from made-up data only (the repository is public):

```bash
python3 scripts/demo_data.py
```

builds a fictional budget in `~/Library/Caches/I Need A Budget-demo` through the real
engine; start the `demo` entry in `.claude/launch.json` (the preview on it, port 8766),
then `python3 scripts/screenshots.py` draws them off screen with WebKit (the list is
`SHOTS` in the script) and saves the PNGs. Retake them when a page changes noticeably.

## Release checklist

Go through all of it for every release. Installed copies update themselves from the latest
release, so a mistake reaches every copy. Merging a pull request doesn't release anything.

**Who does what.** The maintainer only merges PRs (`VERSION` already says the next release's
version) and approves the release run. Claude starts the run, from the Mac or the PC, only when
the maintainer says it's ready (one release for everything merged since the last). GitHub's
machines build, sign, check and publish both apps (`.github/workflows/release.yml`,
[ADR 0009](adr/0009-both-apps-are-built-and-signed-on-github.md)); nobody builds a release on
their own computer.

**What the run does**, stopping at the first problem. Publish needs every job before it to have
passed, on both systems, so a failure anywhere publishes nothing.

1. **What's going out** (Ubuntu): started on `main`; `VERSION` newer than the latest release (the
   one installed copies see); no tag `v<version>` yet; the notes without card numbers.
2. **Tests and builds**, on macOS and Windows, with no secret in reach: the whole suite; the Mac
   app (`scripts/build.sh --adhoc`), which `scripts/check-mac-app.py` checks will start on every
   Mac it says it does: no program or library in it needs a macOS newer than 12
   (`LSMinimumSystemVersion`), whatever macOS, Python and PyInstaller GitHub's Macs have, and each
   is built for the main program's processors; the Windows installer (`scripts/build-windows.sh`),
   with `release-key.pub` in it.
3. **Sign**, after the **Approve**. This is the only job with the `release` environment, so it
   waits until the maintainer approves it (GitHub emails them: the run → Review deployments →
   Approve), and it's the only one whose steps get the secrets (*Release secrets*). It runs only
   the repository's own scripts, Apple's tools and OpenSSL:
   - `scripts/sign-mac.sh`: the certificate into a temporary keychain, the app signed as
     `build.sh` signs, then checked with the requirement installed copies use (team
     `XT4LT7MS6Y`, Apple's chain); the certificate must have 30 or more days left.
   - `scripts/sign-windows.sh`: the installer signed with `RELEASE_KEY`, once its public half is
     known to be `release-key.pub`; the `.sig` then passes the app's own check.
   - The keychain and the key are deleted before the job ends (an `always()` step too). The log
     shows the certificate's team, fingerprint and end date, never its owner's name.
4. **The disk image** (macOS), made without Finder (`scripts/make-dmg.py`, with dmgbuild): the
   app and Applications with the arrow, on the background, with the app's icon as the disk's, in
   the window 1.4.3 had. `hdiutil verify`, then `make-dmg.py --check` reads it back: the window,
   what's in it, the app's version and its signature.
5. **Updates.** The previous release is installed on GitHub's Mac, and `scripts/check-update.py`
   runs that version's own updater, from its tag, on the new disk image, as if it had just
   downloaded it: it must pick it out of the release's three files and accept it. On Windows the
   same, once a release has a Windows installer to install first (1.5.0 is the first). And the
   new installer is installed quietly, as a person would: that copy must have the release key
   and be able to update itself.
6. **Publish** (Ubuntu): `v<version>`, from the exact commit the run built, with the `.dmg`, the
   `Setup.exe` and its `.sig`. Then the API installed copies read must show that tag, at that
   commit, with exactly those three files, byte for byte (if not, the release goes back to a
   draft and the run fails), and the previous release becomes a draft.

**A dry run** does all of it but publish. Started on `main` (`-f dry_run=true`) it signs with the
real secrets, after the Approve, which proves them without releasing; anywhere else it signs with
throwaway identities made in the run (a self-signed certificate standing in for the team, a
throwaway key for the release key) and never reaches the `release` environment. A dry run whose
`VERSION` isn't newer than the latest release builds as the next version, so the update checks
have something newer to check. Its files are in the run's Summary for 7 days.

`gh workflow run` only works for a workflow that's on `main`, so until 1.5.0 is merged the dry
runs come from pull requests and pushes (*Checks on GitHub*).

### Before starting

- [ ] Everything going out is merged into `main`: the run builds `main` as GitHub has it.
- [ ] `VERSION` in `Application Files/main.py` is higher than the latest release (the
  first PR after a release raises it: by 0.0.1, or 0.1 for a big change). The build
  copies it into the app's `Info.plist` and the installer; installed copies only update to a
  higher version.
- [ ] `main`'s Tests checks passed on GitHub (the run runs them again).
- [ ] If the database schema changed: `SCHEMA_VERSION` is raised and `db._migrate` upgrades
  an existing database (test it on a copy of a real one in a throwaway folder).
- [ ] If the updater, packaging or signing changed: a test copy updates to a higher-version
  build and refuses bad ones (*Testing the updater*), and a dry run passed.
- [ ] Release notes: what's new since the last release (every PR merged since), in
  plain words. **No private details**:
  no amounts, payee or people's names, categories or account digits from the
  maintainer's data, even as examples. Write them outside the repository (e.g. the
  scratchpad), not as a file in it, and check them on the computer you start from, before they
  reach GitHub (a run's inputs are public):
  ```bash
  python3 scripts/hooks/privacy_check.py --what "In the release notes" NOTES.md
  ```
  It checks `.git-denylist` where there is one (the Mac). GitHub's machines have none: the run
  checks the notes again there, for card numbers only.

### Start it

```bash
gh workflow run release.yml --repo DigitalN/I-Need-A-Budget --ref main -F notes=@NOTES.md
gh run watch --repo DigitalN/I-Need-A-Budget "$(gh run list --repo DigitalN/I-Need-A-Budget --workflow release.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
```

`-F notes=@NOTES.md` reads the file (in PowerShell, quote it: `-F "notes=@NOTES.md"`). The run
stops at Sign until the maintainer approves it: tell them it's waiting. To try it first:
`gh workflow run release.yml --ref main -f dry_run=true`. A run waiting for the Approve can be
cancelled (`gh run cancel <id>`), and nothing is published.

If a job fails, nothing was published: fix it in a pull request, merge it, and start again.
If GitHub took the release but showed it wrong, Publish says so and makes it a draft again.

### After publishing

- [ ] The run's Summary says Published v<version>, and the release page lists only it, with
  the `.dmg`, the `Setup.exe` and the `.sig`.
- [ ] An older installed copy picks it up: Settings → Check now. `app.log` shows
  `Downloading`, `ready to install`, and on quit `Installed <version>` (on Windows,
  `Installing <version> after quitting`, then `Installed <version>`).
- [ ] The maintainer's own copy updates the same way (within a day, or Settings → Check
  now, then quit). Its log shows `Backed up the budget to …/Documents/I Need A Budget
  Backups/…` on quit (`No backup made: …` if nothing changed since the newest one). If
  the release raised the schema, it also shows `Upgraded the database`, and that folder
  has the pre-upgrade copy.
- [ ] CLAUDE.md and the README describe what's shipping (new features, decisions, gaps).

**History.** 1.0.0 was installed from a local build and never published. 1.0.1
(2026-10-13) was the first release, and 1.0.0 → 1.0.1 the first update installed through
the updater. Until 1.4.3 releases were built and published from the maintainer's Mac by a
script (`scripts/release.sh`); 1.5.0 is the first from the release run. Until a release is
published, Check now just says the app is up to date.

**Lessons:**
- Merging isn't releasing. No release on GitHub means nothing to update to.
- What matters on a Mac is the team (`XT4LT7MS6Y`), not the certificate: a renewed
  certificate from the same Apple account goes on updating every copy (*Release secrets*
  says how to replace it, before it expires on 2027-10-05).
- To launch a built copy for a test: it must be in an Applications folder (elsewhere it
  offers to move itself), so ditto it into `~/Applications/<test folder>/`, start its
  binary directly with `INAB_DATA_DIR=<throwaway>` (not `open`: the same bundle identifier
  can hand off to the maintainer's running copy), kill only that PID, delete the folder.
- The installed copy runs its own version's updater, which is why `check-update.py`
  checks with the code at the installed version's tag.

## How updates work

`Application Files/updater.py`, ported from Pythonflow's, changed for a windowed app
and for a repository that was private at first. The Mac's own steps (the Keychain, the disk
image, `codesign`, swapping the app in, relaunching) are in `computer_mac.py`; Windows' (the
release key, the installer, Credential Manager) in `computer_windows.py` and
`windows_update.py`:

- **Access:** a request carries a token when there is one: the one saved from Settings (on a
  Mac in the Keychain, service "I Need A Budget", account "GitHub"; on Windows in Credential
  Manager, a generic credential "I Need A Budget", user name "GitHub"), else the GitHub CLI's
  (`gh auth token`). Without one it asks anonymously, which is all a public repository
  needs. If GitHub refuses a copy (the repository made private again), it gets "the app's
  repository is private" and Settings shows the GitHub access row (hidden otherwise). A
  fine-grained token with read-only *Contents* access to this one repository is then
  enough. The token reaches curl on stdin (`--config -`),
  never the command line.
- **When:** once, a few seconds after each launch (Settings → "Check for updates at
  launch" turns that off), and when you press Check now. Never on a timer while the app
  is open: the maintainer's choice.
- **What:** the latest release (not drafts or pre-releases) via the GitHub API. Each system
  picks its own file, downloaded through the asset's API URL (curl drops the token when
  GitHub redirects to storage): a Mac the `.dmg` (copies on 1.4.x take the first `.dmg` in a
  release, so a release keeps one), Windows `I-Need-A-Budget-<version>-Setup.exe` and its
  signature `I-Need-A-Budget-<version>-Setup.exe.sig`. A release whose installer has no
  signature is refused before anything is downloaded.
- **Where releases come from:** the release run (*Release checklist*), which builds and signs
  the three files on GitHub's machines and, before publishing them, makes every check below
  itself, with the previous release's own updater code (`scripts/check-update.py`), so a
  release installed copies would refuse never goes out.
- **Checks on a Mac:** the app is copied out of the disk image and `codesign --verify --deep
  --strict` must pass with the requirement
  `identifier "com.digitaln.ineedabudget" and anchor apple generic and certificate leaf[subject.OU] = "<this copy's team>"`.
  The version inside must also be newer.
- **Checks on Windows:** the installer's signature must be our release key's (Ed25519 over
  its exact bytes: the formats are in *Release secrets*), checked with the public half built
  into the app, `release-key.pub` (read at launch; a key of small order, such as a
  placeholder of zeros, is no key, and updates are off). The check is our own short Ed25519
  verify (`windows_update.py`, tested against RFC 8032's vectors), so there's no dependency
  to pin or license to bundle. Nothing in the installer runs before that. Its version is the
  one in its version resource (the signature covers it), not the release's tag, and it must
  be newer; the banner shows that version. Both are read while the file is held so nobody
  can change it, and again just before it runs, on the file that runs.
- **Installing on a Mac:** when the user quits (close button or ⌘Q), with one
  `replaceItemAtURL` swap, or straight away with "Restart now" in the banner. Logging out
  installs it too.
- **Installing on Windows:** at the same moments, but the installer can't replace a running
  app's files: the app logs `Installing 9.9.0 after quitting`, starts a small PowerShell
  script (`windows_update.HELPER_SCRIPT`, no window) and quits. The script waits for it to
  exit (a minute at most), runs the installer with `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART`,
  adds `Installed 9.9.0` (or `Couldn't install 9.9.0: its installer failed (exit code N)`) to
  `app.log`, and for Restart now opens the app again with the same environment,
  `INAB_DATA_DIR` included. An app still running after that minute gets no installer and no
  second copy. The installer itself must not open the app when run quietly.
- **Logging off on Windows:** the installer never starts while Windows logs off or shuts
  down, which could stop it half-way. The checked download waits in the data folder's
  `Update waiting` instead. At the next launch it's checked again (its signature, a version
  newer than the app's) and, before the window opens, installed the way quitting installs it,
  and the app reopens. One that doesn't pass is deleted.
- **Off** for copies run from source; on a Mac, not team-signed, not in an Applications
  folder, or unable to replace themselves; on Windows, without a usable release key, or not
  in `%LOCALAPPDATA%\Programs\I Need A Budget`, where the installer always installs it. A
  copy that can't tell (Windows didn't say where its Programs folder is) is off too, and
  still opens. Settings says why.

## Testing the updater

`python -m pytest scripts/tests/test_updater.py scripts/tests/test_windows_update.py` runs the
checks with stand-ins on any system, and on Windows the whole update too: a stand-in installer
(made with Windows' own C# compiler, so it has a version resource) signed with a throwaway key,
a stand-in feed, and the real helper, at quit and with Restart now. To try it with the real app:

### On a Mac

Use a separate copy with its own bundle identifier, so it can't touch the installed app.

1. Build, then make the test copy and a higher-version update, each re-signed after
   editing its `Info.plist`:
   ```bash
   APP=$(scripts/build.sh)
   ID=$(security find-identity -v -p codesigning | awk '/"Apple Development/ {print $2; exit}')
   mkdir -p /tmp/inab-update /tmp/inab-feed
   copy() {  # destination version
     ditto "$APP" "$1"
     /usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier com.digitaln.ineedabudget.test" \
       -c "Set :CFBundleShortVersionString $2" -c "Set :CFBundleVersion $2" "$1/Contents/Info.plist"
     codesign --force --deep -s "$ID" "$1"
   }
   copy "$HOME/Applications/I Need A Budget Test.app" 1.0
   copy "/tmp/inab-update/I Need A Budget.app" 9.9
   hdiutil create -volname "I Need A Budget" -srcfolder /tmp/inab-update -format UDZO /tmp/inab-feed/update-9.9.dmg
   ```
2. A stand-in for GitHub's answer, `/tmp/inab-feed/latest.json`:
   ```json
   {"tag_name": "v9.9", "assets": [{"name": "I-Need-A-Budget-9.9.dmg", "url": "file:///tmp/inab-feed/update-9.9.dmg"}]}
   ```
3. Run the test copy on its own data folder (with `downloads_dir` pointing somewhere
   harmless) and the stand-in feed:
   ```bash
   mkdir -p /tmp/inab-data/empty && echo '{"downloads_dir": "/tmp/inab-data/empty"}' > /tmp/inab-data/settings.json
   open -n --env INAB_DATA_DIR=/tmp/inab-data --env INAB_UPDATE_FEED=file:///tmp/inab-feed/latest.json \
     "$HOME/Applications/I Need A Budget Test.app"
   ```
   Settings → Check now. `/tmp/inab-data/Logs/app.log` shows `Downloading 9.9` and
   `9.9 is ready to install`; quit and it shows `Installed 9.9`.
4. To check bad updates are refused, change the update's `Info.plist` after signing,
   re-sign it ad hoc (`codesign --force --deep -s -`), or sign it with another bundle
   identifier. Each should log `Update check failed` and leave the test copy alone.
5. Clean up: quit the test copy, then delete it, `/tmp/inab-data`, `/tmp/inab-update`
   and `/tmp/inab-feed`.

### On Windows

A throwaway test key stands in for the release key, so nothing needs the real one. Use a PC
where nobody uses the app for real: the test installs over the copy in your Programs folder
(the data stays apart, in `INAB_DATA_DIR`). In Git Bash, which has OpenSSL 3:

1. A throwaway key, outside the repository, and its public half as the app reads it:
   ```bash
   T="$(cygpath -m "$TEMP")/inab-test"; mkdir -p "$T/feed" "$T/data/empty"
   openssl genpkey -algorithm ed25519 -out "$T/test-key.pem"
   openssl pkey -in "$T/test-key.pem" -pubout -outform DER | tail -c 32 | base64 > "$T/test-key.pub"
   ```
2. Build a low version (1.0, say: the version is `VERSION` in `main.py`), install it with its
   installer, then give that copy the throwaway key in place of its own:
   ```bash
   APP="$(cygpath -m "$LOCALAPPDATA")/Programs/I Need A Budget"
   cp "$T/test-key.pub" "$(find "$APP" -name release-key.pub)"
   ```
3. Build a higher version's installer (9.9) and sign it with the throwaway key, as the release
   run signs with the real one:
   ```bash
   cp <the 9.9 installer> "$T/feed/I-Need-A-Budget-9.9-Setup.exe"
   openssl pkeyutl -sign -rawin -inkey "$T/test-key.pem" -in "$T/feed/I-Need-A-Budget-9.9-Setup.exe" \
     | base64 -w0 > "$T/feed/I-Need-A-Budget-9.9-Setup.exe.sig"
   ```
4. A stand-in for GitHub's answer, `$T/feed/latest.json`:
   ```bash
   cat > "$T/feed/latest.json" <<EOF
   {"tag_name": "v9.9", "assets": [
     {"name": "I-Need-A-Budget-9.9-Setup.exe", "url": "file:///$T/feed/I-Need-A-Budget-9.9-Setup.exe"},
     {"name": "I-Need-A-Budget-9.9-Setup.exe.sig", "url": "file:///$T/feed/I-Need-A-Budget-9.9-Setup.exe.sig"}]}
   EOF
   ```
5. Run the test copy on its own data folder (`downloads_dir` somewhere harmless) and the
   stand-in feed:
   ```bash
   echo "{\"downloads_dir\": \"$T/data/empty\"}" > "$T/data/settings.json"
   INAB_DATA_DIR="$T/data" INAB_UPDATE_FEED="file:///$T/feed/latest.json" "$APP/I Need A Budget.exe" &
   ```
   Settings → Check now. `$T/data/Logs/app.log` shows `Downloading 9.9` and
   `9.9.0 is ready to install`. Quit: it shows `Installing 9.9.0 after quitting`, then
   `Installed 9.9.0`, and Settings → Apps lists 9.9. Do it again from 1.0 with Restart now:
   the app comes back by itself, on `$T/data`. And once more, signing out of Windows with the
   app open: the log says `Not installing 9.9.0 while logging out`, and the next launch
   installs it before the window opens, then reopens.
6. To check bad updates are refused: change the installer after signing (`printf x >>
   "$T/feed/I-Need-A-Budget-9.9-Setup.exe"`), sign it with another throwaway key, leave its
   `.sig` out of `latest.json`, or give the feed a newer tag than the installer's own version.
   Each logs `Update check failed: …`, and nothing is installed.
7. Clean up: uninstall the test copy (Settings → Apps), then delete `$T`.
