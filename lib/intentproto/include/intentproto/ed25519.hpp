#ifndef INTENTPROTO_ED25519_HPP
#define INTENTPROTO_ED25519_HPP
// Ed25519 signature VERIFICATION (RFC 8032), verify-only.
//
// Added for signed firmware images (RFC 0001 doc 11, "Signed images"):
// the bootloader verifies an Ed25519 signature over the application
// image before it will mark it valid or boot it. The device never
// signs and never generates keys — signing happens off-device with the
// private key held on the owner's server (scripts/sign_image.py) — so
// only the verify half lives here, which keeps the MCU code minimal.
//
// Core profile: freestanding C++ — no heap, no exceptions, no RTTI, no
// STL. Fixed-size stack buffers only. Verification operates entirely on
// public data (signature, message, public key), so constant-time
// behaviour is NOT required and NOT attempted.
//
// The field/group arithmetic follows the compact, public-domain
// TweetNaCl reference (D. J. Bernstein et al.); SHA-512 is provided by
// sha512.hpp. Verification uses the (cofactorless) equation
// R == [S]B - [k]A, which is equivalent to the cofactored
// [8][S]B == [8]R + [8][k]A on all RFC 8032 section 7.1 vectors and is
// what this code is tested against.

#include <stddef.h>
#include <stdint.h>

namespace intentproto {

constexpr size_t ED25519_SIG_SIZE = 64;
constexpr size_t ED25519_PUBKEY_SIZE = 32;

// Verify a detached Ed25519 signature. Returns true iff sig is a valid
// signature of msg[0..len) under the public key pub. Malformed public
// keys (points not on the curve) and malformed signatures are rejected
// (returns false), never crash.
bool ed25519_verify(const uint8_t sig[ED25519_SIG_SIZE],
                    const uint8_t* msg, size_t len,
                    const uint8_t pub[ED25519_PUBKEY_SIZE]);

} // namespace intentproto

#endif // INTENTPROTO_ED25519_HPP
