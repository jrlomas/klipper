# Firmware signing keys

Ed25519 keys for signed firmware images (RFC 0001 doc 11,
[docs/rfcs/0001-motion-intentions/11-Bootloader.md](../docs/rfcs/0001-motion-intentions/11-Bootloader.md),
"Signed images"). The bootloader in `src/boot_app` verifies an Ed25519
(RFC 8032) signature over the application image against the public key
embedded in `helix_pubkey.h` before it will mark the image valid or
boot it.

## THESE ARE DEV/TEST KEYS — DO NOT USE IN A RELEASE

**`helix_dev_signing.key` and `helix_dev_signing.pub` are a THROWAWAY
development keypair, committed deliberately so the signing mechanism can
be built, tested, and demonstrated end-to-end without needing the real
key.** They are generated from a fixed, published seed and provide **no
security whatsoever** — the private seed is right here in the repo.

**They MUST be rotated before any real release.** The real release
private key is generated the same way (`scripts/gen_signing_key.py`) but
its private half lives ONLY on the owner's server and is **never
committed**. Rotating is: generate a new keypair off-repo, regenerate
`helix_pubkey.h` from the new `.pub`, ship bootloaders that embed the new
public key, and sign releases with the new private key.

## Files

| File | What it is |
| --- | --- |
| `helix_dev_signing.key` | DEV private seed, 32 bytes as hex. **Throwaway.** |
| `helix_dev_signing.pub` | DEV public key, 32 bytes as hex |
| `helix_pubkey.h` | embedded C public key the bootloader compiles in — GENERATED from the `.pub` |

## Reproducing / rotating

The committed dev keypair was produced deterministically:

```
./scripts/gen_signing_key.py --out-dir keys --name helix_dev_signing \
    --seed d1e5f00d0badc0ffee1234567890abcdef00112233445566778899aabbccddee
./scripts/gen_signing_key.py --pub keys/helix_dev_signing.pub \
    --header keys/helix_pubkey.h
```

For a real key, omit `--seed` (random key), keep the `.key` OFF-repo, and
regenerate the header:

```
# on the owner's server, never committed:
./scripts/gen_signing_key.py --out-dir /secure/keys --name helix_release
# in the repo, only the public half:
./scripts/gen_signing_key.py --pub /secure/keys/helix_release.pub \
    --header keys/helix_pubkey.h
```

## Signing an image

```
# assemble the combined bootloader+app image, then sign it:
make -C src/boot_app combined TARGET=stm32f072 \
    BOOT_BIN=build/boot-f072.bin APP_BIN=../../out/klipper.bin \
    OUT=build/combined-f072.bin
./scripts/sign_image.py combined stm32f072 build/combined-f072.bin \
    --key keys/helix_dev_signing.key -o build/combined-f072.signed.bin
```

or in one step during assembly:

```
./scripts/build_combined.py stm32f072 build/boot-f072.bin ../../out/klipper.bin \
    -o build/combined-f072.signed.bin --sign-key keys/helix_dev_signing.key
```

`scripts/sign_image.py` prefers `pynacl`/`cryptography` if installed and
otherwise uses a vendored pure-Python RFC 8032 signer, so it runs
anywhere; all paths produce signatures the on-device C verifier accepts.
