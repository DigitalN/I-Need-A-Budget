# Both apps are built and signed on GitHub; Windows stays unsigned

Releases are built by GitHub Actions, the Mac app on a GitHub Mac and the Windows installer
on a GitHub Windows machine, so releasing needs neither the maintainer's Mac nor a PC. The
Apple Development certificate (team `XT4LT7MS6Y`), which installed Mac copies require on
an update, is a secret of a protected `release` environment, and so is our own Ed25519
release key, whose public half Windows copies use to check an update. The release run waits
for the maintainer's Approve before it can reach either, and publishes only when both builds
pass every check. This replaces ADR 0003 (on the shelved `claude/windows-support` branch),
which kept Mac builds on the Mac so the certificate never left it.

## Considered options

- Building on GitHub and signing on the Mac: every release would still need the Mac.
- Our own key for Mac updates too: no safer, as it would be on GitHub as well, and every
  installed Mac copy would need one reinstall by hand.
- Signing the Windows installer: SignPath Foundation (free, but at its discretion, and
  published under its name), Azure Artifact Signing (about US$10 a month), Certum's
  open-source certificate (about €49 a year), or the Microsoft Store (free, but every
  release waits for review and the Store does the updating). All but the Store still warn
  about a new app until it builds a reputation. The maintainer chose not to sign.

## Consequences

- Anyone who got into the GitHub account could now ship signed code to every copy; before,
  they would have needed the Mac too. Two-factor sign-in and the environment's required
  approval are the guard.
- The first Windows install shows "Windows protected your PC" (More info → Run anyway).
  Updates don't, because the app downloads them itself.
- A copy of the release key is kept in the maintainer's password manager. If it were lost
  there and on GitHub, Windows copies would need one reinstall by hand from a release
  signed with a new key. The certificate expires 2027-10-05; renewing it means replacing
  the secret.
- Every pull request runs the tests on macOS and Windows and makes unsigned builds of both
  apps to try.
- Decided 2026-11-27, for 1.5.0. Linux stays out.
