#!/usr/bin/env python3
# End-to-end signed-image crosscheck (FD-0001 doc 11, "Signed images"):
# the host signer (scripts/sign_image.py) signs blobs with the COMMITTED
# dev key, and the freestanding on-device C verifier (built here as
# tools/ed25519_verify_cli) must accept exactly those signatures and
# reject tampered ones. This is the proof that the Python signing path
# and the C bootloader verify path agree.
#
# Run standalone:  python3 tools/test_ed25519_e2e.py
# (also invoked by `make test` when python3 is available).
#
# Exits non-zero on any mismatch.

import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
IP = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import sign_image  # noqa: E402

KEY = os.path.join(REPO, "keys", "helix_dev_signing.key")
PUB = os.path.join(REPO, "keys", "helix_dev_signing.pub")


def build_cli(build_dir):
    cli = os.path.join(build_dir, "ed25519_verify_cli")
    cxx = os.environ.get("CXX", "g++")
    cmd = [cxx, "-std=c++17", "-Os", "-Wall", "-Wextra", "-fno-exceptions",
           "-fno-rtti", "-I" + os.path.join(IP, "include"),
           os.path.join(IP, "src", "sha512.cpp"),
           os.path.join(IP, "src", "ed25519.cpp"),
           os.path.join(HERE, "ed25519_verify_cli.cpp"), "-o", cli]
    subprocess.run(cmd, check=True)
    return cli


def verify(cli, pub_hex, sig, msg, tmp):
    sigf = os.path.join(tmp, "sig.bin")
    msgf = os.path.join(tmp, "msg.bin")
    with open(sigf, "wb") as f:
        f.write(sig)
    with open(msgf, "wb") as f:
        f.write(msg)
    r = subprocess.run([cli, pub_hex, sigf, msgf],
                       stdout=subprocess.DEVNULL)
    return r.returncode == 0


def main():
    seed = sign_image.load_key(KEY)
    with open(PUB) as f:
        pub_hex = f.read().split()[0]
    # The committed .pub must match the key the signer derives.
    assert sign_image._public_from_seed(seed).hex() == pub_hex, \
        "committed .pub does not match .key"
    print("signer backend:", sign_image.backend_name())

    # Exercise every available signer backend: whatever library is
    # installed AND the vendored pure-Python fallback, so the crosscheck
    # holds even where the lead has no crypto package.
    backends = [("auto", sign_image._backend)]
    if sign_image._backend is not None:
        backends.append(("pure-python-ref", None))

    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        cli = build_cli(tmp)

        for label, backend in backends:
            saved = sign_image._backend
            sign_image._backend = backend
            try:
                bname = sign_image.backend_name()
                # A spread of sizes, incl. empty and multi-block messages.
                for size in (0, 1, 32, 64, 127, 128, 129, 1000, 4096):
                    msg = bytes((i * 37 + 11) & 0xFF for i in range(size))
                    sig = sign_image.ed25519_sign(seed, msg)

                    if not verify(cli, pub_hex, sig, msg, tmp):
                        print("FAIL[%s]: valid signature rejected, size %d"
                              % (bname, size))
                        failures += 1
                        continue

                    # Flip a byte of the message -> must reject.
                    if size:
                        bad = bytearray(msg)
                        bad[size // 2] ^= 0x01
                        if verify(cli, pub_hex, sig, bytes(bad), tmp):
                            print("FAIL[%s]: tampered message accepted, size %d"
                                  % (bname, size))
                            failures += 1

                    # Flip a byte of the signature -> must reject.
                    badsig = bytearray(sig)
                    badsig[10] ^= 0x01
                    if verify(cli, pub_hex, bytes(badsig), msg, tmp):
                        print("FAIL[%s]: tampered signature accepted, size %d"
                              % (bname, size))
                        failures += 1
                print("  %s signer: sizes 0..4096 accept + tamper-reject OK"
                      % bname)
            finally:
                sign_image._backend = saved

    if failures:
        print("%d FAILURE(S)" % failures)
        return 1
    print("ed25519 python<->C end-to-end: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
