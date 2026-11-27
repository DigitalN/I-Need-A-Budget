# SPDX-License-Identifier: GPL-3.0-only
"""Windows updates, checked with our own release key (issue #188, ADR 0009).

Ed25519 against RFC 8032's own test vectors; what a release has for each system (and what Mac
copies on 1.4.x, running 1.4.3's code, pick from the same release); the checks a downloaded
installer must pass; and, on a PC, the whole update with a stand-in installer: installed once the
app has quit or with Restart now, the app reopened on the same data folder, a changed or unsigned
installer refused. Throwaway keys only: the release key's private half is never near the repository.
"""

import ast
import base64
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import updater
import windows_update
from computer_mac import Mac
from computer_windows import Windows
from updater import UpdateError, parse_release

ROOT = Path(__file__).resolve().parents[2]
WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="runs Windows' own programs")

RFC_8032 = {  # section 7.1: name → (secret key, public key, message, signature), in hex
    "1": (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        ("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
         "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ),
    "2": (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        ("92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
         "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ),
    "3": (
        "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "af82",
        ("6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
         "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
    ),
    "1024": (
        "f5e5767cf153319517630f226876b86c8160cc583bc013744c6bf255f5cc0ee5",
        "278117fc144c72340f67d0f2316e8386ceffbf2b2428c9c51fef7c597f1d426e",
        ("08b8b2b733424243760fe426a4b54908632110a66c2f6591eabd3345e3e4eb98"
         "fa6e264bf09efe12ee50f8f54e9f77b1e355f6c50544e23fb1433ddf73be84d8"
         "79de7c0046dc4996d9e773f4bc9efe5738829adb26c81b37c93a1b270b20329d"
         "658675fc6ea534e0810a4432826bf58c941efb65d57a338bbd2e26640f89ffbc"
         "1a858efcb8550ee3a5e1998bd177e93a7363c344fe6b199ee5d02e82d522c4fe"
         "ba15452f80288a821a579116ec6dad2b3b310da903401aa62100ab5d1a36553e"
         "06203b33890cc9b832f79ef80560ccb9a39ce767967ed628c6ad573cb116dbef"
         "efd75499da96bd68a8a97b928a8bbc103b6621fcde2beca1231d206be6cd9ec7"
         "aff6f6c94fcd7204ed3455c68c83f4a41da4af2b74ef5c53f1d8ac70bdcb7ed1"
         "85ce81bd84359d44254d95629e9855a94a7c1958d1f8ada5d0532ed8a5aa3fb2"
         "d17ba70eb6248e594e1a2297acbbb39d502f1a8c6eb6f1ce22b3de1a1f40cc24"
         "554119a831a9aad6079cad88425de6bde1a9187ebb6092cf67bf2b13fd65f270"
         "88d78b7e883c8759d2c4f5c65adb7553878ad575f9fad878e80a0c9ba63bcbcc"
         "2732e69485bbc9c90bfbd62481d9089beccf80cfe2df16a2cf65bd92dd597b07"
         "07e0917af48bbb75fed413d238f5555a7a569d80c3414a8d0859dc65a46128ba"
         "b27af87a71314f318c782b23ebfe808b82b0ce26401d2e22f04d83d1255dc51a"
         "ddd3b75a2b1ae0784504df543af8969be3ea7082ff7fc9888c144da2af58429e"
         "c96031dbcad3dad9af0dcbaaaf268cb8fcffead94f3c7ca495e056a9b47acdb7"
         "51fb73e666c6c655ade8297297d07ad1ba5e43f1bca32301651339e22904cc8c"
         "42f58c30c04aafdb038dda0847dd988dcda6f3bfd15c4b4c4525004aa06eeff8"
         "ca61783aacec57fb3d1f92b0fe2fd1a85f6724517b65e614ad6808d6f6ee34df"
         "f7310fdc82aebfd904b01e1dc54b2927094b2db68d6f903b68401adebf5a7e08"
         "d78ff4ef5d63653a65040cf9bfd4aca7984a74d37145986780fc0b16ac451649"
         "de6188a7dbdf191f64b5fc5e2ab47b57f7f7276cd419c17a3ca8e1b939ae49e4"
         "88acba6b965610b5480109c8b17b80e1b7b750dfc7598d5d5011fd2dcc5600a3"
         "2ef5b52a1ecc820e308aa342721aac0943bf6686b64b2579376504ccc493d97e"
         "6aed3fb0f9cd71a43dd497f01f17c0e2cb3797aa2a2f256656168e6c496afc5f"
         "b93246f6b1116398a346f1a641f3b041e989f7914f90cc2c7fff357876e506b5"
         "0d334ba77c225bc307ba537152f3f1610e4eafe595f6d9d90d11faa933a15ef1"
         "369546868a7f3a45a96768d40fd9d03412c091c6315cf4fde7cb68606937380d"
         "b2eaaa707b4c4185c32eddcdd306705e4dc1ffc872eeee475a64dfac86aba41c"
         "0618983f8741c5ef68d3a101e8a3b8cac60c905c15fc910840b94c00a0b9d0"),
        ("0aab4c900501b3e24d7cdf4663326a3a87df5e4843b2cbdb67cbf6e460fec350"
         "aa5371b1508f9f4528ecea23c436d94b5e8fcd4f681e30a6ac00a9704a188a03"),
    ),
    "SHA(abc)": (
        "833fe62409237b9d62ec77587520911e9a759cec1d19755b7da901b96dca3d42",
        "ec172b93ad5e563bf4932c70e1245034c35467ef2efd4d64ebf819683467e2bf",
        ("ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a"
         "2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f"),
        ("dc2a4459e7369633a52b1bf277839a00201009a3efbf3ecb69bea2186c26b589"
         "09351fc9ac90b3ecfdfbc7c66431e0303dca179c138ac17ad9bef1177331a704"),
    ),
}


# ── a signer for the tests, made the way the release run signs (RFC 8032 §5.1.6) ──

def _expand(seed):
    digest = hashlib.sha512(seed).digest()
    return int.from_bytes(digest[:32], "little") & ((1 << 254) - 8) | (1 << 254), digest[32:]


def _encode(point):
    z = pow(point[2], windows_update.P - 2, windows_update.P)
    x, y = point[0] * z % windows_update.P, point[1] * z % windows_update.P
    return (y | (x & 1) << 255).to_bytes(32, "little")


def public_key_of(seed):
    return _encode(windows_update._multiply(_expand(seed)[0], windows_update._BASE))


def sign(seed, message):
    a, prefix = _expand(seed)
    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % windows_update.L
    r_encoded = _encode(windows_update._multiply(r, windows_update._BASE))
    k = int.from_bytes(hashlib.sha512(r_encoded + public_key_of(seed) + message).digest(), "little")
    return r_encoded + ((r + k * a) % windows_update.L).to_bytes(32, "little")


def throwaway_key():
    return os.urandom(32)


def vector(name):
    return tuple(bytes.fromhex(part) for part in RFC_8032[name])


def small_order_points():
    """The curve's eight points of small order, written as keys are: [L] of a point whose
    order is 8L generates them."""
    found = set()
    for y in range(2, 200):
        point = windows_update._point(y.to_bytes(32, "little"))
        if point is None:
            continue
        torsion = windows_update._multiply(windows_update.L, point)
        multiple = windows_update._IDENTITY
        for _ in range(8):
            found.add(_encode(multiple))
            multiple = windows_update._add(multiple, torsion)
        if len(found) == 8:
            return sorted(found)
    raise AssertionError("no point of order 8L found")


# ── Ed25519 ─────────────────────────────────────────────────

@pytest.mark.parametrize("name", RFC_8032)
def test_rfc_8032s_test_vectors_verify(name):
    _, public, message, signature = vector(name)
    assert windows_update.verify(public, signature, message)
    assert windows_update.verify(public, signature, [message[:5], message[5:]])  # read in pieces, as a file is


@pytest.mark.parametrize("name", RFC_8032)
def test_the_tests_signer_makes_rfc_8032s_signatures(name):
    """So the throwaway keys below sign exactly as the release key does."""
    secret, public, message, signature = vector(name)
    assert public_key_of(secret) == public and sign(secret, message) == signature


def flipped(data, i):
    return data[:i] + bytes([data[i] ^ 1]) + data[i + 1:]


def test_anything_changed_is_refused():
    _, public, message, signature = vector("3")
    other_public = vector("2")[1]
    assert not windows_update.verify(public, signature, flipped(message, 0))
    assert not windows_update.verify(public, signature, message + b"\0")
    assert not windows_update.verify(other_public, signature, message)         # another key
    assert not windows_update.verify(public, flipped(signature, 3), message)   # its R
    assert not windows_update.verify(public, flipped(signature, 40), message)  # its S
    assert not windows_update.verify(public, signature[:63], message)
    assert not windows_update.verify(public, signature + b"\0", message)
    assert not windows_update.verify(public[:31], signature, message)
    assert not windows_update.verify(public.hex(), signature, message)


def test_a_signature_or_key_written_another_way_is_refused():
    """S + L passes the same equation (RFC 8032 refuses any S of L or more), and a point can be
    written with a y of p or more, or as x = 0 with x's sign set: none of them is a signature or a key."""
    _, public, message, signature = vector("1")
    s = int.from_bytes(signature[32:], "little")
    assert not windows_update.verify(public, signature[:32] + (s + windows_update.L).to_bytes(32, "little"), message)
    for odd in ((windows_update.P + 1).to_bytes(32, "little"),  # the point (0, 1) with y + p
                (1 | 1 << 255).to_bytes(32, "little"),            # (0, 1) with x's sign set
                (2).to_bytes(32, "little")):                      # no point has y = 2
        assert windows_update._point(odd) is None
        assert not windows_update.usable_key(odd)
        assert not windows_update.verify(odd, signature, message)
        assert not windows_update.verify(public, odd + signature[32:], message)


def test_a_key_of_small_order_proves_nothing_and_turns_updates_off(tmp_path):
    """Anyone can sign for one of the curve's eight small-order points without a private key: with
    the identity as the key, R = the identity and S = 0 pass for any file, and 32 zero bytes (a
    placeholder release-key.pub) is a point of order 4 whose forgeries pass a quarter of the time.
    Such a key is refused outright, so a bad key always means updates off."""
    keys = small_order_points()
    zero, identity = bytes(32), (1).to_bytes(32, "little")
    assert zero in keys and identity in keys
    message = b"MZ any file at all"
    for key in keys:
        assert windows_update._point(key) is not None and not windows_update.usable_key(key)
        for r in keys:  # R = a small-order point, S = 0: what a forger tries
            assert not windows_update.verify(key, r + bytes(32), message)
        (tmp_path / "release-key.pub").write_text(base64.b64encode(key).decode() + "\n", encoding="ascii")
        assert windows_update.release_key(tmp_path / "release-key.pub") is None
    for tries in range(64):  # the reviewer's forgery with the zero key: R = rB - jA for the j that passes
        a, r = windows_update._point(zero), int.from_bytes(os.urandom(32), "little") % windows_update.L
        for j in range(4):
            negated = windows_update._multiply((4 - j) % 4, a)
            forged = _encode(windows_update._add(windows_update._multiply(r, windows_update._BASE), negated))
            assert not windows_update.verify(zero, forged + r.to_bytes(32, "little"), message + bytes([tries]))
    assert windows_update.usable_key(vector("1")[1])


def test_a_signature_made_by_openssl_verifies(tmp_path):
    """OpenSSL 3 (Git for Windows has it) is one way to sign: its keys and signatures are the same
    raw bytes (docs/DEVELOPMENT.md → Release secrets)."""
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("no openssl here")
    key, message = tmp_path / "throwaway.pem", tmp_path / "I-Need-A-Budget-9.9-Setup.exe"
    message.write_bytes(b"MZ made-up installer " + os.urandom(1000))
    made = subprocess.run([openssl, "genpkey", "-algorithm", "ed25519", "-out", str(key)], capture_output=True)
    signed = made.returncode == 0 and subprocess.run(
        [openssl, "pkeyutl", "-sign", "-rawin", "-inkey", str(key), "-in", str(message)], capture_output=True)
    if not signed or signed.returncode != 0:
        pytest.skip("this openssl doesn't sign with Ed25519")
    public = subprocess.run([openssl, "pkey", "-in", str(key), "-pubout", "-outform", "DER"],
                            capture_output=True, check=True).stdout[-32:]
    seed = subprocess.run([openssl, "pkey", "-in", str(key), "-outform", "DER"],
                          capture_output=True, check=True).stdout[-32:]
    assert windows_update.verify(public, signed.stdout, message.read_bytes())
    assert public_key_of(seed) == public and sign(seed, message.read_bytes()) == signed.stdout


# ── the release key and an installer's signature ────────────

def test_the_release_key_is_one_line_of_32_bytes_in_base64(tmp_path):
    key, path = public_key_of(throwaway_key()), tmp_path / "release-key.pub"
    line = base64.b64encode(key).decode()
    for text, read in ((line + "\n", key), (line, key), ("", None), (base64.b64encode(key[:31]).decode(), None),
                       ("not base64!", None), (line + "\n" + line, None)):
        path.write_text(text, encoding="ascii")
        assert windows_update.release_key(path) == read, text
    assert windows_update.release_key(tmp_path / "none.pub") is None


def signed_installer(folder, seed, name="I-Need-A-Budget-9.9-Setup.exe", content=None):
    folder.mkdir(parents=True, exist_ok=True)
    installer = folder / name
    installer.write_bytes(content if content is not None else b"MZ made-up installer " + os.urandom(3000))
    windows_update.signature_of(installer).write_text(
        base64.b64encode(sign(seed, installer.read_bytes())).decode() + "\n", encoding="ascii")
    return installer


def test_an_installer_is_checked_with_the_release_key(tmp_path):
    seed = throwaway_key()
    installer = signed_installer(tmp_path, seed)
    windows_update.check(installer, public_key_of(seed))  # signed: no complaint

    with pytest.raises(UpdateError, match="changed or isn't signed with the release key"):
        windows_update.check(installer, public_key_of(throwaway_key()))  # signed with another key
    with installer.open("ab") as f:
        f.write(b"\0")
    with pytest.raises(UpdateError, match="changed or isn't signed with the release key"):
        windows_update.check(installer, public_key_of(seed))  # changed after signing
    windows_update.signature_of(installer).write_text("not a signature", encoding="ascii")
    with pytest.raises(UpdateError, match="changed or isn't signed"):
        windows_update.check(installer, public_key_of(seed))
    windows_update.signature_of(installer).unlink()
    with pytest.raises(UpdateError, match="isn't signed"):
        windows_update.check(installer, public_key_of(seed))
    with pytest.raises(UpdateError, match="no release key"):
        windows_update.check(signed_installer(tmp_path / "other", seed), None)


def test_the_helper_gets_what_it_needs_and_the_app_keeps_its_environment(tmp_path):
    environment = windows_update.helper_environment(
        {"INAB_DATA_DIR": r"C:\made-up\data", "INAB_HELPER_INSTALLER": "left from before", "PATH": "x"},
        wait_for=4321, folder=tmp_path, app=tmp_path / "I Need A Budget", log_file=tmp_path / "app.log")
    assert environment == {"INAB_DATA_DIR": r"C:\made-up\data", "PATH": "x", "INAB_HELPER_WAIT_FOR": "4321",
                           "INAB_HELPER_FOLDER": str(tmp_path), "INAB_HELPER_PATIENCE": "60",
                           "INAB_HELPER_APP": str(tmp_path / "I Need A Budget" / "I Need A Budget.exe"),
                           "INAB_HELPER_LOG": str(tmp_path / "app.log"), "PYINSTALLER_RESET_ENVIRONMENT": "1"}
    with_installer = windows_update.helper_environment({}, wait_for=1, folder=tmp_path, app=tmp_path,
                                                       installer=tmp_path / "update-setup.exe", version="9.9.0")
    assert with_installer["INAB_HELPER_INSTALLER"] == str(tmp_path / "update-setup.exe")
    assert with_installer["INAB_HELPER_VERSION"] == "9.9.0" and "INAB_HELPER_LOG" not in with_installer
    assert windows_update.app_exe(tmp_path / "I Need A Budget.exe") == tmp_path / "I Need A Budget.exe"
    # On powershell.exe's command line as it is: Windows' quoting would change a double quote.
    assert '"' not in windows_update.HELPER_SCRIPT
    assert "'" + "', '".join(windows_update.QUIET) + "'" in windows_update.HELPER_SCRIPT


# ── what a release has for each system ──────────────────────

def asset(name, number):
    return {"name": name, "url": f"https://api.github.com/x/assets/{number}",
            "browser_download_url": f"https://github.com/x/download/{name}"}


RELEASE = {"tag_name": "v1.5.0", "assets": [  # the Windows files first: copies on 1.4.x take the first .dmg
    asset("I-Need-A-Budget-1.5.0-Setup.exe", 1), asset("I-Need-A-Budget-1.5.0-Setup.exe.sig", 2),
    asset("I-Need-A-Budget-1.5.0.dmg", 3)]}


def test_windows_picks_the_installer_and_its_signature_and_the_mac_its_disk_image():
    assert parse_release(RELEASE, Windows.update_file, Windows.update_signature) == (
        "1.5.0", "https://api.github.com/x/assets/1", "https://api.github.com/x/assets/2")
    assert parse_release(RELEASE, Mac.update_file, Mac.update_signature) == (
        "1.5.0", "https://api.github.com/x/assets/3", None)


def test_mac_copies_on_1_4_pick_the_disk_image_from_a_release_with_windows_files_in_it():
    """Installed Mac copies on 1.4.x run their own parse_release: 1.4.3's, from its tag."""
    shown = subprocess.run(["git", "-C", str(ROOT), "show", "v1.4.3:Application Files/updater.py"],
                           capture_output=True, encoding="utf-8")
    assert shown.returncode == 0, f"1.4.3's tag is needed here (git fetch --tags): {shown.stderr}"
    function = next(node for node in ast.parse(shown.stdout).body
                    if isinstance(node, ast.FunctionDef) and node.name == "parse_release")
    code = {"UpdateError": UpdateError}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "v1.4.3:updater.py", "exec"), code)
    assert code["parse_release"](RELEASE) == ("1.5.0", "https://api.github.com/x/assets/3")


def test_a_release_without_a_signed_installer_is_refused_before_downloading():
    unsigned = {"tag_name": "v1.5.0", "assets": [RELEASE["assets"][0], RELEASE["assets"][2]]}
    with pytest.raises(UpdateError, match="installer isn't signed"):
        parse_release(unsigned, Windows.update_file, Windows.update_signature)
    mac_only = {"tag_name": "v1.5.0", "assets": [RELEASE["assets"][2]]}
    with pytest.raises(UpdateError, match="no installer attached"):
        parse_release(mac_only, Windows.update_file, Windows.update_signature)
    signature_of_another = {"tag_name": "v1.5.0", "assets": [
        RELEASE["assets"][0], asset("I-Need-A-Budget-1.4.9-Setup.exe.sig", 4)]}
    with pytest.raises(UpdateError, match="installer isn't signed"):
        parse_release(signature_of_another, Windows.update_file, Windows.update_signature)


# ── an update on Windows, its tools stood in for ────────────

class ThisPC(Windows):
    """Windows' update steps with the test's own Programs folder. Anywhere but Windows, the
    version an installer says it is is the test's, and a file held unchangeable isn't held:
    both are Windows' own APIs, tested on a PC below."""

    def __init__(self, programs, version=None):
        self.programs, self.version = programs, version

    def _programs_folder(self):
        return self.programs

    def _installer_version(self, path):
        return self.version or super()._installer_version(path)

    if sys.platform != "win32":
        def _hold(self, path):
            return "held"

        def _let_go(self, handle):
            pass


def feed_of(installer, tag="v9.9", signed=True):
    assets = [{"name": installer.name, "url": installer.as_uri()}]
    if signed:
        sig = windows_update.signature_of(installer)
        assets.append({"name": sig.name, "url": sig.as_uri()})
    return {"tag_name": tag, "assets": assets}


@pytest.fixture
def pc(tmp_path, monkeypatch):
    """A PC with the app in its Programs folder and a throwaway release key built in, GitHub's
    releases standing in as files, and its temporary folder in the test's."""
    import tempfile

    seed = throwaway_key()
    key = tmp_path / "release-key.pub"
    key.write_text(base64.b64encode(public_key_of(seed)).decode() + "\n", encoding="ascii")
    monkeypatch.setattr(windows_update, "RELEASE_KEY_FILE", key)
    (tmp_path / "temp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "temp"))
    monkeypatch.setenv("INAB_UPDATE_FEED", "file:///feed.json")
    feed, fetched, files = {}, [], {}

    def fake_curl(url, output, max_time, token=None, accept=None, *, curl, no_window):
        fetched.append(url)
        if output == "-":
            return 0, json.dumps(feed)
        shutil.copyfile(files[url], output)
        return 0, ""

    monkeypatch.setattr(updater, "_curl", fake_curl)
    computers = []

    class Settings:
        def get(self):
            return {"update_automatically": True}

    def start(version="1.5.0"):
        """The app launching: a new copy of this computer, as each launch has."""
        computers.append(ThisPC(tmp_path / "Programs", version=computers[-1].version if computers else "9.9.0"))
        return updater.Updater(app, version, settings=Settings(), data_dir=tmp_path / "data",
                               on_status=lambda state: None, computer=computers[-1])

    def publish(installer, **kwargs):
        feed.clear()
        feed.update(feed_of(installer, **kwargs))
        for path in (installer, windows_update.signature_of(installer)):
            files[path.as_uri()] = path

    app = tmp_path / "Programs" / "I Need A Budget"
    app.mkdir(parents=True)
    (tmp_path / "data").mkdir()
    started = []
    monkeypatch.setattr(ThisPC, "_start_helper", lambda self, a, folder, installer, version=None:
                        started.append((folder, installer, version)))
    monkeypatch.setattr(ThisPC, "_session_ending", lambda self: False)
    yield {"seed": seed, "publish": publish, "fetched": fetched, "computers": computers, "app": app, "start": start,
           "started": started, "temp": tmp_path / "temp", "tmp": tmp_path, "kept": tmp_path / "data" / "Update waiting"}
    for computer in computers:  # what swap_in holds until the app exits
        if computer._holding is not None:
            computer._let_go(computer._holding)


def test_a_signed_newer_installer_is_downloaded_and_installed_once_the_app_has_quit(pc, caplog):
    installer = signed_installer(pc["tmp"] / "release", pc["seed"])
    pc["publish"](installer)
    u = pc["start"]()
    assert u.unavailable_reason is None
    with caplog.at_level(logging.INFO, logger="inab"):
        u._check()
        assert u.status == updater.READY and u.version == "9.9.0", caplog.text  # the installer's own, not the tag's
        assert pc["fetched"][1:] == [installer.as_uri(), windows_update.signature_of(installer).as_uri()]
        assert u.install(relaunch_after=True)
    [(folder, moved, version)] = pc["started"]
    assert moved.read_bytes() == installer.read_bytes() and windows_update.signature_of(moved).exists()
    assert version == "9.9.0" and (folder / "relaunch").exists()
    assert [p.name for p in pc["temp"].iterdir()] == [folder.name]  # the download's own folder is gone
    assert "Installing 9.9.0 after quitting; the app reopens then" in caplog.text
    assert "Installed" not in caplog.text  # that's the installer's to say, once it has


REFUSED = {
    "changed": "Update check failed: The downloaded installer was changed or isn't signed with the release key",
    "another key": "Update check failed: The downloaded installer was changed or isn't signed with the release key",
    "unsigned": "Update check failed: The latest release's installer isn't signed",
    "older inside": "Update check failed: The latest release contains version 1.4.3, which isn't newer",
    "not newer": "Up to date at 1.5.0",
}


@pytest.mark.parametrize("what", REFUSED)
def test_an_installer_that_isnt_ours_and_newer_is_refused_and_logged(what, pc, caplog):
    installer = signed_installer(pc["tmp"] / "release", throwaway_key() if what == "another key" else pc["seed"])
    if what == "changed":
        with installer.open("ab") as f:
            f.write(b"\0")
    pc["publish"](installer, tag="v1.5.0" if what == "not newer" else "v9.9", signed=what != "unsigned")
    u = pc["start"]()
    if what == "older inside":
        pc["computers"][-1].version = "1.4.3"  # the release's tag says 9.9; the installer itself, 1.4.3
    with caplog.at_level(logging.INFO, logger="inab"):
        u._check()
    assert REFUSED[what] in caplog.text
    assert u.status == updater.IDLE and not u.install() and not pc["started"]
    assert list(pc["temp"].iterdir()) == []  # nothing left of the download
    if what in ("unsigned", "not newer"):
        assert len(pc["fetched"]) == 1  # refused from the release alone: nothing downloaded


@pytest.mark.parametrize("what", ["changed", "another version", "signature gone"])
def test_the_installer_is_checked_again_just_before_it_runs(what, pc, caplog):
    """Downloaded and checked at launch, run at quit, maybe hours later: anything changed in
    between and it isn't run."""
    pc["publish"](signed_installer(pc["tmp"] / "release", pc["seed"]))
    u = pc["start"]()
    u._check()
    _, (download, _), _ = u._staged
    if what == "changed":
        with download.open("ab") as f:
            f.write(b"\0")
    elif what == "another version":
        pc["computers"][-1].version = "9.8.0"
    else:
        windows_update.signature_of(download).unlink()
    with caplog.at_level(logging.INFO, logger="inab"):
        assert not u.install(relaunch_after=True)
    if what == "signature gone":
        assert u.last_error.startswith("Couldn't start the installer for 9.9.0 (")
    else:
        assert u.last_error == "The installer for 9.9.0 changed after it was checked, so it wasn't run."
    assert "Couldn't install 9.9.0: " + u.last_error in caplog.text
    assert not pc["started"] and list(pc["temp"].iterdir()) == []


def test_an_installer_that_cant_be_started_says_so(pc, monkeypatch):
    pc["publish"](signed_installer(pc["tmp"] / "release", pc["seed"]))
    u = pc["start"]()
    u._check()

    def refused(self, app, folder, installer, version=None):
        raise PermissionError("made-up: access is denied")

    monkeypatch.setattr(ThisPC, "_start_helper", refused)
    assert not u.install()
    assert u.last_error == "Couldn't start the installer for 9.9.0 (made-up: access is denied)."
    assert list(pc["temp"].iterdir()) == []


def test_updates_are_off_without_the_release_key_or_where_the_installer_doesnt_install(pc, tmp_path, monkeypatch):
    programs = tmp_path / "Programs"
    assert pc["start"]().unavailable_reason is None
    assert ThisPC(programs).why_updates_off(programs / "I Need A Budget" / "I Need A Budget.exe") is None
    elsewhere = "I Need A Budget isn't installed in your Programs folder"
    for folder in (tmp_path / "Downloads" / "I Need A Budget", programs, programs / "Other" / "I Need A Budget",
                   programs / "I Need A Budget" / "Inside"):
        assert ThisPC(programs).why_updates_off(folder) == elsewhere, folder
    monkeypatch.setattr(windows_update, "RELEASE_KEY_FILE", tmp_path / "none.pub")
    assert ThisPC(programs).why_updates_off(pc["app"]) == "this copy has no release key to check updates with"
    assert pc["computers"][-1].why_updates_off(pc["app"]) is None  # read at launch: what changes later changes nothing


def test_a_copy_that_cant_tell_whether_it_can_update_still_opens(pc, monkeypatch):
    def unknown(self):
        raise OSError("Windows didn't say where the folder is (made-up)")

    monkeypatch.setattr(ThisPC, "_programs_folder", unknown)
    u = pc["start"]()
    assert u.unavailable_reason == ("couldn't tell whether this copy can update itself "
                                    "(Windows didn't say where the folder is (made-up))")
    with pytest.raises(UpdateError, match="Automatic updates are off: couldn't tell"):
        u.check_now()


@pytest.mark.parametrize("how", ["said so", "Windows said so"])
def test_an_update_waits_through_a_log_off_and_goes_in_at_the_next_launch(how, pc, caplog, monkeypatch):
    """Never installed while Windows logs off or shuts down: a PC that's always shut down with the
    app open would otherwise never update. It's checked again at the next launch, as a download
    is, and goes in before the window opens."""
    pc["publish"](signed_installer(pc["tmp"] / "release", pc["seed"]))
    u = pc["start"]()
    u._check()
    if how == "Windows said so":  # a way of quitting that didn't say: swap_in asks Windows
        monkeypatch.setattr(ThisPC, "_session_ending", lambda self: True)
    with caplog.at_level(logging.INFO, logger="inab"):
        assert not u.install(logging_out=how == "said so")
    assert not pc["started"] and list(pc["temp"].iterdir()) == []
    assert "Not installing 9.9.0 while logging out: it's checked again and installed at the next launch" in caplog.text
    assert sorted(p.name for p in pc["kept"].iterdir()) == ["update-setup.exe", "update-setup.exe.sig"]
    assert not u.install()  # it's been put aside

    monkeypatch.setattr(ThisPC, "_session_ending", lambda self: False)
    later = pc["start"]()  # the next launch
    with caplog.at_level(logging.INFO, logger="inab"):
        assert later.take_kept() and later.install(relaunch_after=True)
    [(folder, moved, version)] = pc["started"]
    assert version == "9.9.0" and (folder / "relaunch").exists() and moved.exists()
    assert "Installing 9.9.0, kept from logging out, before the window opens" in caplog.text
    assert not pc["kept"].exists()


@pytest.mark.parametrize("what", ["changed", "not newer", "updates off"])
def test_an_update_kept_from_a_log_off_that_doesnt_pass_again_is_dropped(what, pc, caplog, monkeypatch):
    pc["publish"](signed_installer(pc["tmp"] / "release", pc["seed"]))
    u = pc["start"]()
    u._check()
    u.install(logging_out=True)
    if what == "changed":
        with (pc["kept"] / "update-setup.exe").open("ab") as f:
            f.write(b"\0")
    if what == "updates off":
        monkeypatch.setattr(windows_update, "RELEASE_KEY_FILE", pc["tmp"] / "none.pub")
    later = pc["start"]("9.9.0" if what == "not newer" else "1.5.0")  # 9.9.0 installed by hand meanwhile
    with caplog.at_level(logging.INFO, logger="inab"):
        assert not later.take_kept()
    assert not pc["started"]
    if what == "updates off":
        assert pc["kept"].exists()  # a copy that can't update leaves it be
        return
    assert not pc["kept"].exists()
    assert "Not installing the update kept from logging out: " + {
        "changed": "The downloaded installer was changed or isn't signed with the release key.",
        "not newer": "its version, 9.9.0, isn't newer than this one"}[what] in caplog.text


def test_a_mac_still_swaps_its_update_in_while_logging_out(tmp_path):
    class SwappingMac(Mac):
        swapped = False

        def swap_in(self, app, update):
            self.swapped = True

    u = updater.Updater.__new__(updater.Updater)
    u._computer, u._app = SwappingMac(), tmp_path / "I Need A Budget.app"
    (tmp_path / "download").mkdir()
    u._staged = ("9.9", tmp_path / "download" / "I Need A Budget.app", tmp_path / "download")
    assert u.install(logging_out=True) and u._computer.swapped


def test_restart_now_without_an_update_still_reopens_the_app(pc):
    pc["start"]()
    pc["computers"][-1].relaunch(pc["app"])
    [(folder, installer, version)] = pc["started"]
    assert installer is None and version is None and (folder / "relaunch").exists()


def test_gh_and_curl_start_without_a_console_window_on_windows(tmp_path, monkeypatch):
    """Console programs started from the windowed app flash a console window up on Windows: gh at
    every launch and whenever Settings opens (Updater.state looks for a token, even with updates
    off), curl at every check. Windows' CREATE_NO_WINDOW stops that; a Mac needs nothing."""
    import types

    ran = []
    monkeypatch.setattr(updater.subprocess, "run", lambda arguments, **kwargs: ran.append(
        kwargs.get("creationflags")) or types.SimpleNamespace(returncode=0, stdout="\n200", stderr=""))
    gh = tmp_path / "gh.exe"
    gh.write_bytes(b"")
    for computer, flags in ((Windows(), 0x08000000), (Mac(), None)):
        computer.gh = (str(gh),)
        ran.clear()
        updater.gh_token(computer)
        updater._curl("https://example.invalid/x", "-", 30, curl=computer.curl, no_window=computer.no_window)
        assert ran == [flags, flags], type(computer).__name__
    if sys.platform == "win32":
        assert Windows.no_window == {"creationflags": subprocess.CREATE_NO_WINDOW}


def test_nothing_the_app_starts_on_windows_flashes_a_console_window():
    """Outside the Mac's own file, every program the app starts says how it starts without a
    window: the computer's no_window, or creationflags of its own."""
    starts = {"subprocess": {"run", "Popen", "call", "check_call", "check_output"}, "os": {"system", "popen"}}
    for path in sorted((ROOT / "Application Files").glob("*.py")):
        if path.name == "computer_mac.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.attr in starts.get(node.func.value.id, ())):
                said = {k.arg for k in node.keywords} | {"no_window" for k in node.keywords
                                                         if k.arg is None and "no_window" in ast.unparse(k.value)}
                assert said & {"creationflags", "no_window"}, f"{path.name}:{node.lineno} starts a program with a window"


# ── on a PC: Windows' own programs ──────────────────────────

STAND_IN = r"""
[assembly: System.Reflection.AssemblyFileVersion("VERSION")]
[assembly: System.Reflection.AssemblyInformationalVersion("VERSION")]
public static class StandIn {
    // Notes its name, what it was run with and the data folder it was given. As the installer
    // (its name ends in -setup.exe), its exit code is STANDIN_EXIT.
    public static int Main(string[] args) {
        string me = System.IO.Path.GetFileName(System.Reflection.Assembly.GetEntryAssembly().Location);
        string note = System.Environment.GetEnvironmentVariable("STANDIN_NOTE");
        if (note != null) {
            System.IO.File.AppendAllText(note, me + "|" + string.Join(" ", args) + "|"
                                         + System.Environment.GetEnvironmentVariable("INAB_DATA_DIR") + "\n");
        }
        string code = System.Environment.GetEnvironmentVariable("STANDIN_EXIT");
        bool installer = me.EndsWith("-setup.exe", System.StringComparison.OrdinalIgnoreCase);
        return installer && code != null ? int.Parse(code) : 0;
    }
}
"""


def make_stand_in(path, version):
    """A program with a version resource, which is what an installer is to the updater, made with
    Windows' own C# compiler (PowerShell's Add-Type)."""
    source = path.with_suffix(".cs")
    source.write_text(STAND_IN.replace("VERSION", version), encoding="utf-8")
    environment = dict(os.environ, STANDIN_SOURCE=str(source), STANDIN_OUT=str(path))
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                    "Add-Type -TypeDefinition ([IO.File]::ReadAllText($env:STANDIN_SOURCE)) "
                    "-OutputAssembly $env:STANDIN_OUT -OutputType ConsoleApplication"],
                   env=environment, check=True, capture_output=True, timeout=120)
    return path


@pytest.fixture(scope="module")
def stand_in(tmp_path_factory):
    return make_stand_in(tmp_path_factory.mktemp("stand-in") / "stand-in.exe", "9.9.0.0")


@WINDOWS
def test_an_installers_version_is_read_from_its_version_resource(stand_in, tmp_path):
    assert Windows()._installer_version(stand_in) == "9.9.0"
    plain = tmp_path / "plain.exe"
    plain.write_bytes(b"not a program")
    assert Windows()._installer_version(plain) is None


@WINDOWS
def test_a_held_installer_can_be_read_but_not_changed_moved_or_deleted(tmp_path):
    installer = tmp_path / "update-setup.exe"
    installer.write_bytes(b"MZ made-up installer")
    pc = Windows()
    handle = pc._hold(installer)
    try:
        assert installer.read_bytes() == b"MZ made-up installer"
        for change in (lambda: installer.open("ab"), installer.unlink, lambda: installer.rename(tmp_path / "other.exe")):
            with pytest.raises(PermissionError):
                change()
    finally:
        pc._let_go(handle)
    with installer.open("ab") as f:  # let go: anything goes again
        f.write(b"\0")
    with pytest.raises(OSError):
        pc._hold(tmp_path / "none.exe")


@WINDOWS
def test_a_token_is_kept_in_credential_manager():
    import ctypes

    pc = Windows()
    pc._CREDENTIAL = f"I Need A Budget test {os.getpid()} {time.time_ns()}"  # never the real one
    token = "github_pat_" + "A1_" * 20
    try:
        assert pc.saved_token() is None
        try:
            pc.save_token(token)
        except UpdateError:
            if ctypes.get_last_error() == 1312:  # ERROR_NO_SUCH_LOGON_SESSION: a service's account
                pytest.skip("this logon session has no credential store")
            raise
        assert pc.saved_token() == token and updater.find_token(pc) == (token, "the token saved in Settings")
        with pytest.raises(UpdateError, match="doesn't look like a GitHub token"):
            pc.save_token('abc" ; del * "' + "a" * 30)
        pc.save_token("")
        assert pc.saved_token() is None
    finally:
        pc.save_token("")


# The app, as far as updating goes: the real updater and Windows' real steps (curl, the release key,
# the helper), with the test's Programs folder and release key. Each run is one launch, which then
# quits the way it's told: "quit", "Restart now", "log off" (Windows ending the session), "launch"
# (installing what a log-off kept, as main.py does before the window), "reopen only" (Restart now
# with no update) or "linger" (Restart now, but the app doesn't exit until well after the helper
# stops waiting). Then it exits, as the app does once it has quit.
APP = r"""
import logging, os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import updater, windows_update
from computer_windows import Windows

data = Path(os.environ["INAB_DATA_DIR"])
(data / "Logs").mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s",
                    handlers=[logging.FileHandler(data / "Logs" / "app.log", encoding="utf-8")])
windows_update.RELEASE_KEY_FILE = Path(os.environ["TEST_RELEASE_KEY"])
programs = Path(os.environ["TEST_PROGRAMS"])
how = sys.argv[2]

class ThisPC(Windows):
    def _programs_folder(self):
        return programs

class Settings:
    def get(self):
        return {"update_automatically": True}

app = programs / "I Need A Budget"
u = updater.Updater(app, "1.5.0", settings=Settings(), data_dir=data, on_status=lambda state: None,
                    computer=ThisPC())
assert u.unavailable_reason is None, u.unavailable_reason
if how == "reopen only":
    u._computer.relaunch(app)
elif how == "launch":
    assert u.take_kept() and u.install(relaunch_after=True)
else:
    u._check()
    if how == "linger":
        windows_update.PATIENCE = 2
    u.install(relaunch_after=how in ("Restart now", "linger"), logging_out=how == "log off")
    if how == "linger":
        time.sleep(8)
"""
REOPENS = ("Restart now", "launch", "reopen only")


def update_on_this_pc(tmp_path, stand_in, *hows, change=None, exit_code=0):
    """Launches the app's update once for each of `hows`, with a stand-in feed and installer, and
    waits for each run's helper; returns what the stand-ins noted and the app's log."""
    seed = throwaway_key()
    key, data, temp = tmp_path / "release-key.pub", tmp_path / "data folder", tmp_path / "temp"
    key.write_text(base64.b64encode(public_key_of(seed)).decode() + "\n", encoding="ascii")
    temp.mkdir()
    programs = tmp_path / "Programs"
    (programs / "I Need A Budget").mkdir(parents=True)
    shutil.copyfile(stand_in, programs / "I Need A Budget" / "I Need A Budget.exe")  # the installed app
    installer = signed_installer(tmp_path / "feed", seed, content=stand_in.read_bytes())
    if change == "changed":
        with installer.open("ab") as f:
            f.write(b"\0")
    feed = tmp_path / "feed" / "latest.json"
    feed.write_text(json.dumps(feed_of(installer, signed=change != "unsigned")), encoding="utf-8")
    note = tmp_path / "note.txt"
    environment = dict(os.environ, INAB_DATA_DIR=str(data), INAB_UPDATE_FEED=feed.as_uri(), TEMP=str(temp),
                       TMP=str(temp), TEST_RELEASE_KEY=str(key), TEST_PROGRAMS=str(programs), STANDIN_NOTE=str(note),
                       STANDIN_EXIT=str(exit_code))
    script = tmp_path / "app.py"
    script.write_text(APP, encoding="utf-8")

    def noted():
        return note.read_text(encoding="utf-8").splitlines() if note.exists() else []

    for how in hows:
        reopened = sum(line.startswith("I Need A Budget.exe|") for line in noted())
        subprocess.run([sys.executable, str(script), str(ROOT / "Application Files"), how], env=environment,
                       check=True, timeout=120)
        deadline = time.monotonic() + 120
        while any(temp.iterdir()) and time.monotonic() < deadline:  # the helper deletes its folder last
            time.sleep(0.2)
        assert not any(temp.iterdir()), "the helper didn't finish"
        if how in REOPENS:
            while time.monotonic() < deadline and sum(line.startswith("I Need A Budget.exe|")
                                                      for line in noted()) == reopened:
                time.sleep(0.2)
        else:
            time.sleep(2)  # long enough for an app the helper shouldn't have opened to note itself
    return noted(), (data / "Logs" / "app.log").read_text(encoding="utf-8")


def messages(log):
    return [line.split("  ", 2)[-1].strip() for line in log.splitlines()]


@WINDOWS
@pytest.mark.parametrize("how", ["quit", "Restart now"])
def test_on_a_pc_an_update_installs_once_the_app_has_quit(how, tmp_path, stand_in):
    noted, log = update_on_this_pc(tmp_path, stand_in, how)
    data = str(tmp_path / "data folder")
    installed = f"update-setup.exe|/VERYSILENT /SUPPRESSMSGBOXES /NORESTART|{data}"
    assert noted == ([installed, f"I Need A Budget.exe||{data}"] if how == "Restart now" else [installed])
    assert messages(log) == ["Downloading 9.9", "9.9.0 is ready to install",
                             "Installing 9.9.0 after quitting" + ("; the app reopens then" if how == "Restart now" else ""),
                             "Installed 9.9.0"], log


@WINDOWS
def test_on_a_pc_an_update_waits_through_a_log_off_and_goes_in_at_the_next_launch(tmp_path, stand_in):
    noted, log = update_on_this_pc(tmp_path, stand_in, "log off", "launch")
    data = str(tmp_path / "data folder")
    assert noted == [f"update-setup.exe|/VERYSILENT /SUPPRESSMSGBOXES /NORESTART|{data}", f"I Need A Budget.exe||{data}"]
    assert messages(log) == ["Downloading 9.9", "9.9.0 is ready to install",
                             "Not installing 9.9.0 while logging out: it's checked again and installed at the next launch",
                             "Installing 9.9.0, kept from logging out, before the window opens",
                             "Installing 9.9.0 after quitting; the app reopens then", "Installed 9.9.0"], log


@WINDOWS
def test_on_a_pc_an_installer_that_fails_is_logged_and_the_app_still_reopens(tmp_path, stand_in):
    noted, log = update_on_this_pc(tmp_path, stand_in, "Restart now", exit_code=3)
    assert [line.split("|")[0] for line in noted] == ["update-setup.exe", "I Need A Budget.exe"]
    assert messages(log)[-1] == "Couldn't install 9.9.0: its installer failed (exit code 3)", log


@WINDOWS
def test_on_a_pc_an_app_that_doesnt_exit_gets_no_installer_and_no_second_copy(tmp_path, stand_in):
    noted, log = update_on_this_pc(tmp_path, stand_in, "linger")
    assert noted == []
    assert messages(log)[-1] == "Couldn't install 9.9.0: the app was still running 2 seconds after quitting", log


@WINDOWS
def test_on_a_pc_restart_now_without_an_update_only_reopens_the_app(tmp_path, stand_in):
    noted, log = update_on_this_pc(tmp_path, stand_in, "reopen only")
    assert noted == [f"I Need A Budget.exe||{tmp_path / 'data folder'}"]
    assert log == ""  # the helper had nothing to install, and says nothing


@WINDOWS
@pytest.mark.parametrize("change", ["changed", "unsigned"])
def test_on_a_pc_a_changed_or_unsigned_installer_is_refused(change, tmp_path, stand_in):
    noted, log = update_on_this_pc(tmp_path, stand_in, "quit", change=change)
    assert noted == []  # nothing ran
    assert messages(log)[-1] == "Update check failed: " + (
        "The downloaded installer was changed or isn't signed with the release key." if change == "changed"
        else "The latest release's installer isn't signed."), log
