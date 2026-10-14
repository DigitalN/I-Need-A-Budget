# Windows builds come from GitHub Actions and are trusted through our own release key

The Mac app is built and signed on the maintainer's Mac with their Apple Development
certificate, and installed Mac copies only take updates signed by that team. Neither half
of that carries over to Windows: PyInstaller can't build a Windows app on a Mac, and
Windows can't check an Apple signature. So GitHub Actions builds the Windows installer
(free for a public repository), and `release.sh` on the Mac signs it with a release key
of our own (Ed25519), kept in the Mac's Keychain with a copy in the maintainer's
1Password. Installed Windows copies refuse an update that the key didn't sign. The key
never goes to GitHub.

## Considered options

- Building the Mac app in GitHub Actions too: the Apple certificate would have to be
  stored on GitHub as a secret.
- Microsoft's Artifact Signing (about US$10 a month): it only quiets the warning on first
  install, which a new app can get even when signed. The maintainer wanted it free.
- Trusting whatever GitHub serves over HTTPS: anyone who got into the GitHub account could
  ship code to every copy.

## Consequences

- Windows sees the installer as unsigned: the first install shows "Windows protected your
  PC" (More info → Run anyway). Updates don't, because the app downloads them itself.
- If the key is lost from both the Keychain and 1Password, Windows copies stop updating
  themselves and need one reinstall by hand from a release signed with a new key.
- A release is published only when both the Mac and the Windows builds succeed: every
  version exists for both.
- Decided 2026-10-14, for 1.2.0. Linux was considered and dropped.
