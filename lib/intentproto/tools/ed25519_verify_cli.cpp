// Tiny CLI around the freestanding Ed25519 verifier, for the
// Python<->C end-to-end signed-image crosscheck (tools/test_ed25519_e2e.py):
// the Python signer (scripts/sign_image.py) signs a blob with the
// committed dev key, and this proves the on-device C verifier accepts
// exactly those signatures (and rejects tampered ones).
//
//   ed25519_verify_cli <pubkey-hex-64> <sig-file-64B> <msg-file>
// exits 0 if the signature is valid, 1 if not, 2 on usage/IO error.

#include "intentproto/ed25519.hpp"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int read_file(const char* path, uint8_t** out, size_t* len) {
    FILE* f = fopen(path, "rb");
    if (!f)
        return -1;
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (n < 0) {
        fclose(f);
        return -1;
    }
    uint8_t* buf = (uint8_t*)malloc((size_t)n ? (size_t)n : 1);
    if (n && fread(buf, 1, (size_t)n, f) != (size_t)n) {
        fclose(f);
        free(buf);
        return -1;
    }
    fclose(f);
    *out = buf;
    *len = (size_t)n;
    return 0;
}

int main(int argc, char** argv) {
    if (argc != 4) {
        fprintf(stderr, "usage: %s <pubkey-hex> <sig-file> <msg-file>\n",
                argv[0]);
        return 2;
    }
    if (strlen(argv[1]) != 64) {
        fprintf(stderr, "pubkey must be 64 hex chars\n");
        return 2;
    }
    uint8_t pub[32];
    for (int i = 0; i < 32; i++) {
        unsigned v = 0;
        if (sscanf(argv[1] + 2 * i, "%2x", &v) != 1)
            return 2;
        pub[i] = (uint8_t)v;
    }

    uint8_t* sig = nullptr;
    size_t siglen = 0;
    uint8_t* msg = nullptr;
    size_t msglen = 0;
    if (read_file(argv[2], &sig, &siglen) || siglen != 64) {
        fprintf(stderr, "signature file must be 64 bytes\n");
        return 2;
    }
    if (read_file(argv[3], &msg, &msglen))
        return 2;

    bool ok = intentproto::ed25519_verify(sig, msg, msglen, pub);
    free(sig);
    free(msg);
    printf(ok ? "VALID\n" : "INVALID\n");
    return ok ? 0 : 1;
}
