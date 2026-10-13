# Developing I Need A Budget

Notes for building from source and publishing releases. For using it, see the
[README](README.md). The architecture and the rules the code follows are in
[CLAUDE.md](CLAUDE.md).

## Build from source

You need Python 3 from [python.org](https://www.python.org/downloads/) and the pinned
packages in `Application Files/requirements.txt` (`First Time Setup.command` installs
them and switches on the privacy hook). Then:

```bash
scripts/install.sh --open
```

This builds `I Need A Budget.app`, signs it with your **Apple Development** certificate,
installs it to `/Applications` and opens it. `Build I Need A Budget.command` does the same
after pulling the latest code and running the tests.

Without a certificate the build is signed ad hoc: it works, but it can't update itself.
A free Apple ID is enough to get one: Xcode → Settings → Apple Accounts → sign in →
select your **Personal Team** → Manage Certificates… → **+** → **Apple Development**.

Builds go to `~/Library/Caches/I Need A Budget-build`, not the repository: folders synced
by iCloud Drive (such as `~/Documents`) add Finder metadata to app bundles, and codesign
rejects bundles that have it.

To run from source without building (uses your real data unless you say otherwise):

```bash
cd "Application Files" && INAB_DATA_DIR=/tmp/inab-test python3 main.py
```

`INAB_DATA_DIR` points at a throwaway data folder; `INAB_TODAY=YYYY-MM-DD` pins today's
date; `INAB_DEBUG=1` turns on the web inspector. Tests: `python3 -m pytest`.

The app icon is drawn by `scripts/make-icon.swift`. To redraw it:

```bash
swift scripts/make-icon.swift /tmp/INeedABudget.iconset && iconutil -c icns /tmp/INeedABudget.iconset -o "Application Files/INeedABudget.icns"
```

## The dev preview

```bash
python3 scripts/preview.py [--data FOLDER] [--today 2026-10-12]
```

Serves `index.html` on http://127.0.0.1:8765 with a stand-in for pywebview's bridge, so
the real page drives the real engine (`BudgetService.dispatch`) over localhost. Use it to
look at and click through the interface in a browser (Claude: the `preview` entry in
`.claude/launch.json`, in the browser pane). It's a development tool only, never bundled,
and it refuses to use the app's real data folder. The default data folder is
`~/Library/Caches/I Need A Budget-preview`; put copies of bank files in a `downloads`
folder there and set `downloads_dir` in its `settings.json` to try onboarding.

## Release checklist

Go through all of it for every release. Installed copies update themselves from the
latest release, so a mistake reaches every copy. Merging a pull request doesn't release
anything.

**Who does what.** The maintainer only merges PRs (each PR already raises `VERSION`).
A merged PR is the go-ahead: Claude releases it from this Mac, where the signing
certificate is, with **`scripts/release.sh NOTES.md`** (notes written outside the repo).
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
- [ ] `VERSION` in `Application Files/main.py` is higher than the latest release (every
  PR raises it: by 0.0.1, or 0.1 for a big change). The build copies it into the app's
  `Info.plist`; installed copies only update to a higher version.
- [ ] `python3 -m pytest` passes (including `test_privacy.py`).
- [ ] The signing certificate is valid and not about to expire:
  `security find-identity -v -p codesigning` lists an *Apple Development* identity from
  team `XT4LT7MS6Y`. Installed copies refuse updates signed by any other team.
- [ ] If the database schema changed: `SCHEMA_VERSION` is raised and `db._migrate` upgrades
  an existing database (test it on a copy of a real one in a throwaway folder).

### Build and check the disk image

- [ ] `scripts/package.sh` makes `dist/I-Need-A-Budget-<version>.dmg`. That's the only
  file a release has. The first time, macOS asks to let Terminal control Finder (it lays
  out the disk image window): allow it.  `dist/` is ignored by git.
- [ ] Open the DMG: the app and Applications with the arrow between them.
- [ ] The app inside has the new version, a valid signature, and the right team:
  ```bash
  M=$(hdiutil attach -nobrowse -readonly -noautoopen dist/I-Need-A-Budget-1.0.0.dmg | grep -o '/Volumes/.*$')
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

- [ ] Release notes: what's new in this version, in plain words. **No private details**:
  no amounts, payee or people's names, categories or account digits from the
  maintainer's data, even as examples. Write them outside the repository (e.g. the
  scratchpad), not as a file in it.
- [ ] Publish it, tagged `v<version>`, from the commit it was built from:
  ```bash
  gh release create v1.0.0 dist/I-Need-A-Budget-1.0.0.dmg --title "I Need A Budget 1.0.0" --notes-file notes.md
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
  now, then quit). If the release raised the schema, its log shows `Upgraded the
  database` and `Backups/` has the pre-upgrade copy.
- [ ] CLAUDE.md and the README describe what's shipping (new features, decisions, gaps).

## How updates work

`Application Files/updater.py`, ported from Pythonflow's, changed for a private
repository and a windowed app:

- **Access:** a request carries a token when there is one: the one saved in the Keychain
  from Settings (service "I Need A Budget", account "GitHub"), else the GitHub CLI's
  (`gh auth token`). Without one it asks anonymously, which works once the repository is
  public. While it's private, a copy without access gets "the app's repository is
  private" and Settings shows the GitHub access row (hidden otherwise). A fine-grained
  token with read-only *Contents* access to this one repository is enough; that's what a
  second Mac or a second person uses. The token reaches curl on stdin (`--config -`),
  never the command line.
- **When:** once, a few seconds after each launch (Settings → "Check for updates at
  launch" turns that off), and when you press Check now. Never on a timer while the app
  is open: the maintainer's choice.
- **What:** the latest release (not drafts or pre-releases) via the GitHub API; its `.dmg`
  is downloaded through the asset's API URL (curl drops the token when GitHub redirects
  to storage).
- **Checks:** the app is copied out of the disk image and `codesign --verify --deep
  --strict` must pass with the requirement
  `identifier "com.digitaln.ineedabudget" and anchor apple generic and certificate leaf[subject.OU] = "<this copy's team>"`.
  The version inside must also be newer.
- **Installing:** when the user quits (close button or ⌘Q), with one `replaceItemAtURL`
  swap, or straight away with "Restart now" in the banner.
- **Off** for copies run from source, not team-signed, not in an Applications folder, or
  unable to replace themselves. Settings says why.

## Testing the updater

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
