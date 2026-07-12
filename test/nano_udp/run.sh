#!/bin/bash
# Build and run the nano_udp host unit test (FD-0001 doc 07 RMII path).
set -eu
cd "$(dirname "$0")/../.."
CC=${CC:-cc}
OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT
$CC -DNANO_UDP_TEST -Wall -Wextra -Isrc -Isrc/generic \
    test/nano_udp/nano_udp_test.c src/generic/nano_udp.c -o "$OUT/nano_udp_test"
"$OUT/nano_udp_test"
