// intentproto dictionary dump tool (host-side build step).
//
// Prints the uncompressed identify dictionary JSON for every
// declaration linked into this binary — a data->data serialization
// of the static registry, exactly what the firmware would serve.
// tools/mkdict.py runs it, zlib-compresses the output, and emits the
// identify_blob.h header assigned to Config::identify_blob.
//
// Firmware projects compile THIS FILE together with THEIR
// declaration translation units (and src/proto.cpp + src/dict.cpp)
// so the emitted dictionary matches the shipped registry:
//
//     g++ -Ilib/intentproto/include lib/intentproto/src/proto.cpp
//         lib/intentproto/src/dict.cpp fw/commands.cpp
//         lib/intentproto/tools/dump_dict.cpp -o dump_dict
//     lib/intentproto/tools/mkdict.py ./dump_dict -o build/
//
// The declarations below are examples so the standalone `make dict`
// target has something to serialize; replace them by linking real
// TUs (each declaration must live in exactly one TU — see
// method.hpp).
//
// Usage: dump_dict [version [build_version]]

#include "intentproto/method.hpp"

#include <stdio.h>

KLIPPER_CONSTANT(CLOCK_FREQ, 48000000);
KLIPPER_CONSTANT_STR(MCU, "intentproto-example");

KLIPPER_ENUMERATION(static_string_id, example_error, 1);

KLIPPER_RESPONSE(example_status, (uint8_t, oid), (uint32_t, value));

KLIPPER_METHOD(example_set_value, (uint8_t, oid), (uint32_t, value)) {
    (void)oid;
    (void)value;
}

KLIPPER_METHOD(example_write, (uint8_t, oid), (intentproto::buf, data)) {
    (void)oid;
    (void)data;
}

static int null_write(const uint8_t*, size_t, void*) { return 0; }

int main(int argc, char** argv) {
    intentproto::Config cfg;
    cfg.write = null_write;
    if (argc > 1)
        cfg.version = argv[1];
    if (argc > 2)
        cfg.build_version = argv[2];
    intentproto::init(cfg);

    static char json[1 << 16];
    size_t n = intentproto::build_dictionary(json, sizeof(json));
    if (!n) {
        fprintf(stderr, "dump_dict: dictionary exceeds %zu bytes\n",
                sizeof(json));
        return 1;
    }
    fwrite(json, 1, n, stdout);
    putchar('\n');
    return 0;
}
