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
`build.sh`, `package.sh`, `install.sh` and `release.sh` take one lock
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

Go through all of it for every release. Installed copies update themselves from the
latest release, so a mistake reaches every copy. Merging a pull request doesn't release
anything.

**Who does what.** The maintainer only merges PRs (`VERSION` already says the next
release's version). Claude releases from this Mac, where the signing certificate is, only
when the maintainer says it's ready (one release for everything merged since the last),
with **`scripts/release.sh NOTES.md`** (notes written outside the repo).
It does every step below that a script can: main matches GitHub, version newer than the
latest release, tests, certificate valid 30+ days, build, `hdiutil verify`,
`scripts/check-update.py` (the installed copy's own updater code, from its tag, accepts
the image), publish from the exact commit, check what installed copies see, draft the
previous release. `scripts/release.sh --dry-run` runs all of it except publishing, on any
branch. The maintainer never builds anything.

**History.** 1.0.0 was installed from a local build and never published. 1.0.1
(2026-10-13) was the first release, and 1.0.0 → 1.0.1 the first update installed through
the updater: `Downloading`, `ready to install`, `Installed 1.0.1; relaunching`, then the
schema upgrade with its backup. Until a release is published, Check now just says the app
is up to date (1.0.0 and 1.0.1 also said that when the token couldn't see the repository;
1.0.2 reports that as an error).

**Lessons from 1.0.1:**
- Merging isn't releasing. No release on GitHub means nothing to update to.
- Several Apple Development certificates can be in the keychain; an expired one is
  harmless. What matters is the one `security find-identity -v -p codesigning` lists
  (team `XT4LT7MS6Y`, valid until 2027-10-05 as of 1.0.1). Renew it before then.
- To launch a built copy for a test: it must be in an Applications folder (elsewhere it
  offers to move itself), so ditto it into `~/Applications/<test folder>/`, start its
  binary directly with `INAB_DATA_DIR=<throwaway>` (not `open`: the same bundle identifier
  can hand off to the maintainer's running copy), kill only that PID, delete the folder.
- The installed copy runs its own version's updater, which is why `check-update.py`
  checks with the code at the installed version's tag.

### Before building

- [ ] Everything going out is merged into `main`, and the local `main` matches GitHub
  (`git pull`).
- [ ] `VERSION` in `Application Files/main.py` is higher than the latest release (the
  first PR after a release raises it: by 0.0.1, or 0.1 for a big change). The build
  copies it into the app's `Info.plist`; installed copies only update to a higher version.
- [ ] `python3 -m pytest` passes (including `test_privacy.py`), and so did `main`'s Tests
  checks on GitHub.
- [ ] The signing certificate is valid and not about to expire:
  `security find-identity -v -p codesigning` lists an *Apple Development* identity from
  team `XT4LT7MS6Y`. Installed copies refuse updates signed by any other team.
- [ ] If the database schema changed: `SCHEMA_VERSION` is raised and `db._migrate` upgrades
  an existing database (test it on a copy of a real one in a throwaway folder).

### Build and check the disk image

- [ ] `scripts/package.sh` makes `~/Library/Caches/I Need A Budget-dmg/I-Need-A-Budget-<version>.dmg`
  (outside the repository, and only the newest). That's the only file a release has. The
  first time, macOS asks to let Terminal control Finder (it lays out the disk image
  window): allow it.
- [ ] Open the DMG: the app and Applications with the arrow between them.
- [ ] The app inside has the new version, a valid signature, and the right team:
  ```bash
  M=$(hdiutil attach -nobrowse -readonly -noautoopen ~/Library/Caches/"I Need A Budget-dmg"/I-Need-A-Budget-1.0.0.dmg | grep -o '/Volumes/.*$')
  defaults read "$M/I Need A Budget.app/Contents/Info" CFBundleShortVersionString
  codesign --verify --deep --strict "$M/I Need A Budget.app" && codesign -dvv "$M/I Need A Budget.app" 2>&1 | grep TeamIdentifier
  hdiutil detach "$M"
  ```

### Test updating, if the updater, packaging or signing changed

- [ ] A test copy updates to a higher-version build from a stand-in release (see
  *Testing the updater* below).
- [ ] It refuses a build changed after signing, one signed ad hoc or by another team, and
  a release whose tag is newer than the app inside it.
- [ ] Afterwards, the test copy, its data folder and the test disk images are deleted.

### Publish

- [ ] Release notes: what's new since the last release (every PR merged since), in
  plain words. **No private details**:
  no amounts, payee or people's names, categories or account digits from the
  maintainer's data, even as examples. Write them outside the repository (e.g. the
  scratchpad), not as a file in it.
- [ ] Publish it, tagged `v<version>`, from the commit it was built from:
  ```bash
  gh release create v1.0.0 ~/Library/Caches/"I Need A Budget-dmg"/I-Need-A-Budget-1.0.0.dmg --title "I Need A Budget 1.0.0" --notes-file notes.md
  ```
  Drafts and pre-releases are skipped by the updater.
- [ ] Turn the previous release into a draft so the page lists only the current one:
  `gh release edit v0.9.0 --draft=true`.

### After publishing

- [ ] The API the updater reads shows the new tag with only the `.dmg`:
  ```bash
  gh api repos/DigitalN/I-Need-A-Budget/releases/latest --jq '.tag_name, [.assets[].name]'
  ```
- [ ] An older installed copy picks it up: Settings → Check now. `app.log` shows
  `Downloading`, `ready to install`, and on quit `Installed <version>`.
- [ ] The maintainer's own copy updates the same way (within a day, or Settings → Check
  now, then quit). Its log shows `Backed up the budget to …/Documents/I Need A Budget
  Backups/…` on quit (`No backup made: …` if nothing changed since the newest one). If
  the release raised the schema, it also shows `Upgraded the database`, and that folder
  has the pre-upgrade copy.
- [ ] CLAUDE.md and the README describe what's shipping (new features, decisions, gaps).

## Release secrets

Release runs (#190) sign both apps on GitHub ([ADR 0009](adr/0009-both-apps-are-built-and-signed-on-github.md))
with secrets kept in a protected GitHub environment. `scripts/release-secrets.sh` sets them up.
Only the maintainer runs it, in their own terminal. It never prints a secret, writes one into the
repository or passes one on a command line, so Claude never sees one.

| Name | What it is | Format |
|---|---|---|
| Environment `release` | Holds the secrets. A run reaches them only after the maintainer's Approve | Required reviewer DigitalN; self-reviews allowed, because Claude starts runs with the maintainer's own `gh` login; administrators can't bypass it; deploys from `main` only |
| `RELEASE_KEY` | Our Ed25519 release key, which signs the Windows installer | The raw 32-byte private key (RFC 8032's seed), base64, one line of 44 characters |
| `Application Files/release-key.pub` | Its public half. It's built into the app, and Windows copies check updates with it. Not secret, so it's in the repository | The raw 32-byte public key, base64, one line |
| `MAC_CERTIFICATE` | The Apple Development identity (certificate and private key, team `XT4LT7MS6Y`) | Keychain Access's `.p12` export, base64, one line |
| `MAC_CERTIFICATE_PASSWORD` | The `.p12`'s password | Made up by the script; it exists only on GitHub |

The signature beside a Windows installer, `I-Need-A-Budget-<version>-Setup.exe.sig`, is Ed25519
(RFC 8032, not pre-hashed) over the installer's exact bytes: the 64-byte signature in base64, one
line. Python's `cryptography` takes the raw keys as they are (`Ed25519PrivateKey.from_private_bytes`,
`Ed25519PublicKey.from_public_bytes`). OpenSSL 3 takes them as DER, with hex
`302e020100300506032b657004220420` before the private key and `302a300506032b6570032100` before the
public one. The release run makes its temporary keychain's password itself, so that isn't a secret.

- `scripts/release-secrets.sh` (or `check`) shows what's set up and what's left. It shows no
  secret, and GitHub never returns their values anyway.
- `scripts/release-secrets.sh key`, on any computer: creates the environment (or fixes its
  settings) and makes the release key with OpenSSL 3 (Git for Windows has it; on a Mac, `brew
  install openssl@3`). The key is made in memory. It goes onto the clipboard, never shown, for the
  password manager's item "I Need A Budget release key". The maintainer pastes the saved copy back
  (hidden) to check it. Only then does the key go into `RELEASE_KEY` and its public half into the
  repository, and Claude commits that. The private key never touches a file, so nothing can be
  synced or left behind. The clipboard is cleared afterwards. Windows' clipboard history (Win+V) keeps its own
  copy: the script says so when it's on.
- `scripts/release-secrets.sh certificate`, on the Mac, with macOS's own tools: checks
  that the keychain has a valid Apple Development certificate of team `XT4LT7MS6Y` and that it has
  30 or more days left. Keychain Access then exports it as a `.p12` into a folder of its own,
  outside iCloud, with a password the script makes up and puts on the clipboard. The script checks
  the `.p12` (the same certificate, with its private key), puts it and its password into the
  environment, then deletes it, and the folder too if you stop.
- `scripts/release-secrets.sh selftest` checks the formats: RFC 8032's test vector, and a
  throwaway key that signs a file, a changed file and another key refused. No GitHub, no
  secrets.

**Renewing the certificate** before it expires on 2027-10-05 (release runs refuse one with fewer
than 30 days left). On the Mac: Xcode → Settings → Accounts → the Apple ID → Manage Certificates… →
**+** → Apple Development. Then run `scripts/release-secrets.sh certificate`, which replaces both
secrets. Installed Mac copies keep updating, because they check the team, not the certificate. An
expired certificate left in the keychain is harmless.

**The release key** is kept in the maintainer's password manager as well as on GitHub. If the
environment loses it, `scripts/release-secrets.sh key` puts the password manager's copy back,
checking it against `release-key.pub` first. If both copies were lost, Windows copies could never
update again on their own. You'd delete `release-key.pub`, run `key` for a new key, commit the new
public half and release, and every Windows copy would need that release installed by hand once.
Never make a new key otherwise: every Windows copy refuses a release signed with a key it doesn't
have. Mac copies aren't affected.

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
