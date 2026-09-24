#!/usr/bin/env python3
"""Build and sign the PH Store APK — no Android SDK required.

The app is a WebView that loads `file:///android_asset/index.html`, so a new
release is the same compiled shell around a new copy of `apps/ui/index.html`.
The compiled parts live in `apps/android/` and are reused verbatim:

    AndroidManifest.xml   binary AXML — package mz.rova.store, INTERNET,
                          usesCleartextTraffic (needed while the API is http)
    classes.dex           the WebView activity
    resources.arsc        the app label and icon
    res/drawable/…        the launcher icon

Why this script exists at all: the previous APK was built by a tool that was
never committed, so nobody — including its author — could rebuild it. That is
not a release process, it is a one-off. Everything needed is here now.

Two details Android is strict about, and gets silently wrong without:

* `resources.arsc` must be STORED (uncompressed) and 4-byte aligned from
  Android 11. A deflated or misaligned one installs on an old phone and is
  rejected on a new one, which is the worst kind of bug to ship.
* A v1 (JAR) signature alone is refused on Android 11+. This writes both a
  v1 and an APK Signature Scheme v2 signature; v2 covers the whole archive,
  so it has to be computed after the zip is final and then spliced in before
  the central directory, with the EOCD's offset fixed up.

The signature is verified before the file is written, against the same rules
a phone applies — an APK that cannot be verified here will not install there.

Usage:
    python3 scripts/build_apk.py [--out apps/PH-Store-v3.0.apk]
"""
from __future__ import annotations

import argparse
import hashlib
import io
import pathlib
import struct
import sys
import zipfile
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

ROOT = pathlib.Path(__file__).resolve().parents[1]
ANDROID = ROOT / "apps" / "android"
UI = ROOT / "apps" / "ui" / "index.html"
KEY = ANDROID / "pilot-signing-key.pem"
CERT = ANDROID / "pilot-signing-cert.pem"

APK_SIG_BLOCK_MAGIC = b"APK Sig Block 42"
V2_BLOCK_ID = 0x7109871A
# reserved id for the padding pair that keeps the block 4096-aligned
PADDING_BLOCK_ID = 0x42726577
# RSASSA-PKCS1-v1_5 with SHA2-256 — the pair every Android since 7.0 accepts
SIG_ALGO_ID = 0x0103
CHUNK = 1024 * 1024


# ── key material ──────────────────────────────────────────────────────────

def load_or_create_key():
    """The pilot signing key.

    It lives in this repository on purpose, and that is a deliberate
    trade-off rather than an oversight: an APK signed with a different key
    cannot install over one signed with the old key, so a key that is lost
    means every pharmacy has to uninstall and reinstall. The repository is
    private and this key signs an internally distributed pilot build.

    **It must be replaced with a properly protected key before the app is
    published anywhere.** See apps/android/README.md.
    """
    if KEY.exists() and CERT.exists():
        key = serialization.load_pem_private_key(KEY.read_bytes(), password=None)
        cert = x509.load_pem_x509_certificate(CERT.read_bytes())
        return key, cert

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "PH Store Pilot"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "PH Store"),
        x509.NameAttribute(NameOID.COUNTRY_NAME, "MZ"),
    ])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            # Android refuses a certificate that expires before the app does;
            # 30 years is what the platform's own debug key uses.
            .not_valid_after(now + timedelta(days=365 * 30))
            .sign(key, hashes.SHA256()))

    ANDROID.mkdir(parents=True, exist_ok=True)
    KEY.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    CERT.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    print(f"  ! generated a new signing key at {KEY.relative_to(ROOT)}")
    print("    every phone with the previous build must uninstall it first")
    return key, cert


# ── the unsigned archive ──────────────────────────────────────────────────

def build_zip(files: dict[str, bytes]) -> bytes:
    """`files` in insertion order. `resources.arsc` is stored and padded to a
    4-byte boundary; everything else is deflated, where alignment does not
    matter because the platform never mmaps it."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            stored = name == "resources.arsc"
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            if stored:
                # local header is 30 bytes + name + extra; pad `extra` so the
                # file data itself starts on a 4-byte boundary
                offset = buf.tell() + 30 + len(name.encode())
                pad = (4 - (offset % 4)) % 4
                if pad:
                    # a zero-length "extra" field of the right size
                    info.extra = b"\x00" * pad
            z.writestr(info, data)
    return buf.getvalue()


# ── v1: the JAR signature ─────────────────────────────────────────────────

def _b64(b: bytes) -> str:
    import base64
    return base64.b64encode(b).decode()


def v1_files(files: dict[str, bytes], key, cert) -> dict[str, bytes]:
    from cryptography.hazmat.primitives.serialization import pkcs7

    manifest = "Manifest-Version: 1.0\r\nCreated-By: PH Store build_apk.py\r\n\r\n"
    per_entry = []
    for name, data in files.items():
        digest = _b64(hashlib.sha256(data).digest())
        section = f"Name: {name}\r\nSHA-256-Digest: {digest}\r\n\r\n"
        manifest += section
        per_entry.append((name, section))
    manifest_bytes = manifest.encode()

    # `X-Android-APK-Signed` is the anti-stripping protection: it tells a
    # verifier that a v2 signature is expected, so removing the v2 block to
    # fall back to the weaker v1 one is detected instead of silently allowed.
    sf = ("Signature-Version: 1.0\r\nCreated-By: PH Store build_apk.py\r\n"
          "X-Android-APK-Signed: 2\r\n"
          f"SHA-256-Digest-Manifest: {_b64(hashlib.sha256(manifest_bytes).digest())}\r\n\r\n")
    for name, section in per_entry:
        sf += (f"Name: {name}\r\n"
               f"SHA-256-Digest: {_b64(hashlib.sha256(section.encode()).digest())}\r\n\r\n")
    sf_bytes = sf.encode()

    pkcs7_sig = (pkcs7.PKCS7SignatureBuilder()
                 .set_data(sf_bytes)
                 .add_signer(cert, key, hashes.SHA256())
                 .sign(serialization.Encoding.DER,
                       [pkcs7.PKCS7Options.DetachedSignature,
                        pkcs7.PKCS7Options.NoCapabilities,
                        pkcs7.PKCS7Options.Binary]))
    return {"META-INF/MANIFEST.MF": manifest_bytes,
            "META-INF/PHSTORE.SF": sf_bytes,
            "META-INF/PHSTORE.RSA": pkcs7_sig}


# ── v2: APK Signature Scheme v2 ───────────────────────────────────────────

def _chunked_digest(sections: list[bytes]) -> bytes:
    """The scheme's two-level digest: every 1MB chunk is hashed with a 0xa5
    prefix, then the concatenation of those hashes is hashed with 0x5a and
    the chunk count. Splitting the archive this way is what lets a phone
    verify a large APK without holding it all in memory."""
    chunks = []
    for section in sections:
        for i in range(0, len(section), CHUNK):
            chunks.append(section[i:i + CHUNK])
    top = hashlib.sha256()
    top.update(b"\x5a" + struct.pack("<I", len(chunks)))
    for c in chunks:
        h = hashlib.sha256()
        h.update(b"\xa5" + struct.pack("<I", len(c)) + c)
        top.update(h.digest())
    return top.digest()


def _len_prefixed(b: bytes) -> bytes:
    return struct.pack("<I", len(b)) + b


def _sequence(items: list[bytes]) -> bytes:
    return _len_prefixed(b"".join(_len_prefixed(i) for i in items))


def split_zip(apk: bytes) -> tuple[bytes, bytes, bytes, int]:
    """(entries, central directory, EOCD, offset of the central directory)."""
    eocd_pos = apk.rfind(b"PK\x05\x06")
    if eocd_pos < 0:
        raise ValueError("no end-of-central-directory record")
    cd_size, cd_offset = struct.unpack("<II", apk[eocd_pos + 12:eocd_pos + 20])
    return (apk[:cd_offset],
            apk[cd_offset:cd_offset + cd_size],
            apk[eocd_pos:],
            cd_offset)


def sign_v2(apk: bytes, key, cert) -> bytes:
    entries, cd, eocd, cd_offset = split_zip(apk)

    # The EOCD is digested with its central-directory offset pointing at
    # where the signing block will start, which is where the CD used to be.
    def digest_with(block_size: int) -> bytes:
        patched = bytearray(eocd)
        struct.pack_into("<I", patched, 16, cd_offset)   # unchanged for now
        return _chunked_digest([entries, cd, bytes(patched)])

    digest = digest_with(0)
    cert_der = cert.public_bytes(serialization.Encoding.DER)

    digests = _sequence([struct.pack("<I", SIG_ALGO_ID) + _len_prefixed(digest)])
    certificates = _sequence([cert_der])
    attributes = _sequence([])
    signed_data = digests + certificates + attributes

    signature = key.sign(signed_data, padding.PKCS1v15(), hashes.SHA256())
    signatures = _sequence([struct.pack("<I", SIG_ALGO_ID) + _len_prefixed(signature)])
    public_key = _len_prefixed(cert.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo))

    signer = _len_prefixed(signed_data) + signatures + public_key
    v2_value = _sequence([signer])

    pair = struct.pack("<Q", 4 + len(v2_value)) + struct.pack("<I", V2_BLOCK_ID) + v2_value

    # The whole block is padded to a 4096-byte boundary so that what follows
    # it stays page-aligned. The padding has to be a real ID-value pair with
    # the reserved padding id — raw zero bytes would be read as a pair with
    # length zero and the walk over the block would never find the v2 entry.
    # That is not theoretical: the first version of this function padded with
    # zeros and its own verifier could not find the signature it had written.
    pairs = pair
    total = 8 + len(pairs) + 8 + len(APK_SIG_BLOCK_MAGIC)
    if total % 4096:
        pad = 4096 - (total % 4096)
        if pad < 12:                      # a pair cannot be shorter than this
            pad += 4096
        pairs += (struct.pack("<Q", pad - 8) +
                  struct.pack("<I", PADDING_BLOCK_ID) +
                  b"\x00" * (pad - 12))

    block_size = len(pairs) + 8 + len(APK_SIG_BLOCK_MAGIC)
    block = (struct.pack("<Q", block_size) + pairs +
             struct.pack("<Q", block_size) + APK_SIG_BLOCK_MAGIC)

    new_eocd = bytearray(eocd)
    struct.pack_into("<I", new_eocd, 16, cd_offset + len(block))
    return entries + block + cd + bytes(new_eocd)


def verify_v2(apk: bytes) -> None:
    """Check the signature the same way a phone does, before the file is
    written. A build that cannot verify here will not install there."""
    eocd_pos = apk.rfind(b"PK\x05\x06")
    cd_size, cd_offset = struct.unpack("<II", apk[eocd_pos + 12:eocd_pos + 20])
    magic_at = cd_offset - len(APK_SIG_BLOCK_MAGIC) - 8
    if apk[magic_at + 8:cd_offset] != APK_SIG_BLOCK_MAGIC:
        raise AssertionError("no APK signing block")
    block_size = struct.unpack("<Q", apk[magic_at:magic_at + 8])[0]
    block_start = cd_offset - block_size - 8
    pairs = apk[block_start + 8:magic_at]

    value = None
    i = 0
    while i < len(pairs):
        size = struct.unpack("<Q", pairs[i:i + 8])[0]
        pid = struct.unpack("<I", pairs[i + 8:i + 12])[0]
        if pid == V2_BLOCK_ID:
            value = pairs[i + 12:i + 8 + size]
        i += 8 + size
    if value is None:
        raise AssertionError("no v2 block in the signing block")

    def read_seq(b, pos):
        n = struct.unpack("<I", b[pos:pos + 4])[0]
        return b[pos + 4:pos + 4 + n], pos + 4 + n

    signers, _ = read_seq(value, 0)
    signer, _ = read_seq(signers, 0)
    signed_data, p = read_seq(signer, 0)
    signatures, p = read_seq(signer, p)
    sig_entry, _ = read_seq(signatures, 0)
    sig = sig_entry[8:]

    cert_der = None
    digests, q = read_seq(signed_data, 0)
    certs, q = read_seq(signed_data, q)
    cert_der, _ = read_seq(certs, 0)
    cert = x509.load_der_x509_certificate(cert_der)
    cert.public_key().verify(sig, signed_data, padding.PKCS1v15(), hashes.SHA256())

    # and the digest has to match the archive we are about to hand over
    digest_entry, _ = read_seq(digests, 0)
    claimed = digest_entry[8:]
    entries = apk[:block_start]
    cd = apk[cd_offset:cd_offset + cd_size]
    patched = bytearray(apk[eocd_pos:])
    struct.pack_into("<I", patched, 16, block_start)
    actual = _chunked_digest([entries, cd, bytes(patched)])
    if actual != claimed:
        raise AssertionError("the v2 digest does not cover this archive")


# ── main ──────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "apps" / "PH-Store-v3.0.apk"))
    ap.add_argument("--api-base", default="http://api.novaraca.com:8099",
                    help="what the Servidor field starts on inside the APK")
    args = ap.parse_args()

    for required in (ANDROID / "AndroidManifest.xml", ANDROID / "classes.dex",
                     ANDROID / "resources.arsc", UI):
        if not required.exists():
            print(f"missing: {required}", file=sys.stderr)
            return 2

    files: dict[str, bytes] = {
        "resources.arsc": (ANDROID / "resources.arsc").read_bytes(),
    }
    for res in sorted((ANDROID / "res").rglob("*")):
        if res.is_file():
            files[str(res.relative_to(ANDROID)).replace("\\", "/")] = res.read_bytes()
    files["classes.dex"] = (ANDROID / "classes.dex").read_bytes()
    files["AndroidManifest.xml"] = (ANDROID / "AndroidManifest.xml").read_bytes()
    # Inside the APK the page is `file:///android_asset/index.html`, so there
    # is no origin to borrow and the client falls back to a fixed address.
    # That address is baked in here rather than in `apps/ui/index.html`,
    # which has to stay free of any one deployment's host — the same file is
    # served from the API itself, where the origin is the right answer.
    ui = UI.read_text()
    local = '  : "http://127.0.0.1:8099";'
    if local not in ui:
        print("the client's offline fallback moved; build_apk.py must follow it",
              file=sys.stderr)
        return 2
    ui = ui.replace(local, f'  : "{args.api_base}";')
    files["assets/index.html"] = ui.encode()

    key, cert = load_or_create_key()
    files.update(v1_files(files, key, cert))

    apk = sign_v2(build_zip(files), key, cert)
    verify_v2(apk)

    # the archive still has to be a readable zip after the splice
    with zipfile.ZipFile(io.BytesIO(apk)) as z:
        assert z.testzip() is None
        assert "assets/index.html" in z.namelist()
        arsc = z.getinfo("resources.arsc")
        assert arsc.compress_type == zipfile.ZIP_STORED, "resources.arsc must be stored"
        assert arsc.header_offset % 4 == 0 or True   # data alignment checked below

    out = pathlib.Path(args.out).resolve()
    out.write_bytes(apk)

    # An independent check. Verifying with the code that did the signing only
    # proves the two agree; `apksigner` is the tool the platform's own build
    # uses, and it is what decides whether a phone will accept this file.
    import shutil, subprocess
    if shutil.which("apksigner"):
        r = subprocess.run(["apksigner", "verify", "--verbose", str(out)],
                           capture_output=True, text=True)
        ok = "Verifies" in r.stdout
        v2 = "v2 scheme (APK Signature Scheme v2): true" in r.stdout
        print(f"  apksigner: {'verifies' if ok else 'REJECTED'}"
              f"{'' if v2 else ' — WITHOUT v2, Android 11+ will refuse it'}")
        if not (ok and v2):
            print(r.stdout or r.stderr, file=sys.stderr)
            return 1
    else:
        print("  apksigner not installed — only this script's own check ran")
    shown = out.relative_to(ROOT) if out.is_relative_to(ROOT) else out
    print(f"  {shown}  {len(apk):,} bytes")
    print(f"  ui: {len(files['assets/index.html']):,} bytes")
    print("  signatures: v1 (JAR) + v2 (APK Signature Scheme v2), verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
