#!/usr/bin/env python3
# Sign a firmware image with an Ed25519 private key for signed firmware
# images (FD-0001 doc 11, "Signed images").
#
# The bootloader (src/boot_app) verifies an Ed25519 signature over the
# exact application image bytes — the same bytes the CRC covers — before
# it marks the image valid or boots it. Signing happens off-device with
# the private key; only the public key is embedded on the MCU.
#
# Signed-image layout (matches boot_flash.h / build_combined.py):
#   the validity record is {magic, size, crc, flags} at info_off; the
#   64-byte signature is stored immediately after it (info_off + 16).
#   flags bit 0 (BOOT_INFO_FLAG_SIGNED) is set for a signed image.
#
# Usage:
#   ./scripts/sign_image.py combined <target> <combined.bin> \
#       --key keys/helix_dev_signing.key -o signed.bin
#       sign an assembled combined image in the layout above
#
#   ./scripts/sign_image.py blob <file> --key <key> -o <file.sig>
#       emit a detached 64-byte Ed25519 signature over raw bytes
#       (used by the Python<->C end-to-end crosscheck)
#
# The signer prefers `cryptography` or `pynacl` when importable; if
# neither is present it falls back to a vendored pure-Python RFC 8032
# implementation (below), so it runs anywhere with just the stdlib. All
# three produce identical signatures the freestanding C verifier accepts.

import argparse
import hashlib
import struct
import zlib

# ---------------------------------------------------------------------------
# Vendored pure-Python Ed25519 (RFC 8032), adapted from the public-domain
# reference implementation. Deterministic; used only when no compiled
# Ed25519 library is available.
# ---------------------------------------------------------------------------

_b = 256
_q = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493


def _H(m):
    return hashlib.sha512(m).digest()


def _inv(x):
    return pow(x, _q - 2, _q)


_d = -121665 * _inv(121666) % _q
_I = pow(2, (_q - 1) // 4, _q)


def _xrecover(y):
    xx = (y * y - 1) * _inv(_d * y * y + 1)
    x = pow(xx, (_q + 3) // 8, _q)
    if (x * x - xx) % _q != 0:
        x = (x * _I) % _q
    if x % 2 != 0:
        x = _q - x
    return x


_By = 4 * _inv(5) % _q
_Bx = _xrecover(_By)
_B = [_Bx % _q, _By % _q]


def _edwards(P, Q):
    x1, y1 = P
    x2, y2 = Q
    x3 = (x1 * y2 + x2 * y1) * _inv(1 + _d * x1 * x2 * y1 * y2)
    y3 = (y1 * y2 + x1 * x2) * _inv(1 - _d * x1 * x2 * y1 * y2)
    return [x3 % _q, y3 % _q]


def _scalarmult(P, e):
    if e == 0:
        return [0, 1]
    Q = _scalarmult(P, e // 2)
    Q = _edwards(Q, Q)
    if e & 1:
        Q = _edwards(Q, P)
    return Q


def _encodeint(y):
    return y.to_bytes(32, "little")


def _encodepoint(P):
    x, y = P
    bits = y | ((x & 1) << 255)
    return bits.to_bytes(32, "little")


def _bit(h, i):
    return (h[i // 8] >> (i % 8)) & 1


def _clamp(h):
    a = 2 ** (_b - 2)
    for i in range(3, _b - 2):
        a += 2 ** i * _bit(h, i)
    return a


def ed25519_publickey(seed):
    h = _H(seed)
    a = _clamp(h)
    A = _scalarmult(_B, a)
    return _encodepoint(A)


def _Hint(m):
    h = _H(m)
    return sum(2 ** i * _bit(h, i) for i in range(2 * _b))


def _ed25519_sign_ref(seed, msg):
    h = _H(seed)
    a = _clamp(h)
    pub = _encodepoint(_scalarmult(_B, a))
    r = _Hint(h[_b // 8:_b // 4] + msg)
    R = _scalarmult(_B, r)
    Rbytes = _encodepoint(R)
    S = (r + _Hint(Rbytes + pub + msg) * a) % _L
    return Rbytes + _encodeint(S)


# ---------------------------------------------------------------------------
# Preferred fast paths, if a compiled library is importable.
# ---------------------------------------------------------------------------

def _try_pynacl():
    try:
        from nacl.signing import SigningKey  # type: ignore
    except Exception:
        return None

    def sign(seed, msg):
        return bytes(SigningKey(seed).sign(msg).signature)

    def pub(seed):
        return bytes(SigningKey(seed).verify_key)

    return sign, pub


def _try_cryptography():
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey)  # type: ignore
        from cryptography.hazmat.primitives import serialization  # type: ignore
    except Exception:
        return None

    def sign(seed, msg):
        return Ed25519PrivateKey.from_private_bytes(seed).sign(msg)

    def pub(seed):
        k = Ed25519PrivateKey.from_private_bytes(seed)
        return k.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    return sign, pub


_backend = _try_pynacl() or _try_cryptography()


def ed25519_sign(seed, msg):
    """Detached Ed25519 signature (64 bytes) over msg with a 32-byte seed."""
    if _backend is not None:
        return _backend[0](seed, msg)
    return _ed25519_sign_ref(seed, msg)


def _public_from_seed(seed):
    if _backend is not None:
        return _backend[1](seed)
    return ed25519_publickey(seed)


def backend_name():
    if _backend is None:
        return "pure-python-ref"
    mod = _backend[0].__module__ if hasattr(_backend[0], "__module__") else ""
    return "pynacl" if "nacl" in repr(_backend) else "library"


# ---------------------------------------------------------------------------
# Image signing.
# ---------------------------------------------------------------------------

BOOT_INFO_MAGIC = 0x50414F42  # "BOAP", matches boot_flash.h
BOOT_INFO_FLAG_SIGNED = 0x00000001
BOOT_INFO_SIG_OFFSET = 16
SIG_SIZE = 64
FLASH_BASE = 0x08000000

# Mirror of scripts/build_combined.py GEOM (per-target flash geometry).
GEOM = {
    "stm32f072": {"app_base": 0x08004000, "info_addr": 0x0801F800},
    "stm32f4": {"app_base": 0x08008000, "info_addr": 0x08060000},
}


def load_key(path):
    with open(path) as f:
        seed = bytes.fromhex(f.read().split()[0])
    if len(seed) != 32:
        raise SystemExit("private key must be a 32-byte seed (64 hex chars)")
    return seed


def sign_combined(target, image, seed):
    """Sign an assembled combined image in place; return the new bytes."""
    g = GEOM[target]
    app_off = g["app_base"] - FLASH_BASE
    info_off = g["info_addr"] - FLASH_BASE
    img = bytearray(image)

    magic, size, crc, flags = struct.unpack_from("<IIII", img, info_off)
    if magic != BOOT_INFO_MAGIC:
        raise SystemExit("no validity record at info offset 0x%x "
                         "(build the combined image first)" % info_off)
    app = bytes(img[app_off:app_off + size])
    if zlib.crc32(app) & 0xFFFFFFFF != crc:
        raise SystemExit("record CRC does not match the application bytes")

    sig = ed25519_sign(seed, app)
    if len(sig) != SIG_SIZE:
        raise SystemExit("unexpected signature length %d" % len(sig))

    flags |= BOOT_INFO_FLAG_SIGNED
    struct.pack_into("<IIII", img, info_off, magic, size, crc, flags)
    img[info_off + BOOT_INFO_SIG_OFFSET:
        info_off + BOOT_INFO_SIG_OFFSET + SIG_SIZE] = sig
    return bytes(img), {"size": size, "crc": crc, "sig": sig.hex(),
                        "pub": _public_from_seed(seed).hex()}


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("combined", help="sign an assembled combined image")
    c.add_argument("target", choices=sorted(GEOM))
    c.add_argument("image")
    c.add_argument("--key", required=True)
    c.add_argument("-o", "--output", required=True)

    b = sub.add_parser("blob", help="detached signature over raw bytes")
    b.add_argument("file")
    b.add_argument("--key", required=True)
    b.add_argument("-o", "--output", required=True)

    args = ap.parse_args()
    seed = load_key(args.key)

    if args.cmd == "combined":
        with open(args.image, "rb") as f:
            image = f.read()
        out, info = sign_combined(args.target, image, seed)
        with open(args.output, "wb") as f:
            f.write(out)
        print("signed combined image: %s (%s, signer=%s)"
              % (args.output, args.target, backend_name()))
        print("  application: %d bytes (crc32=0x%08x)"
              % (info["size"], info["crc"]))
        print("  pubkey     : %s" % info["pub"])
        print("  signature  : %s" % info["sig"])
    else:
        with open(args.file, "rb") as f:
            data = f.read()
        sig = ed25519_sign(seed, data)
        with open(args.output, "wb") as f:
            f.write(sig)
        print("signed %s (%d bytes, signer=%s) -> %s"
              % (args.file, len(data), backend_name(), args.output))


if __name__ == "__main__":
    main()
