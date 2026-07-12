// intentproto extension self-description tests (FD-0001 doc 10).
//
// Drives list_extensions / list_constants end-to-end in loopback:
// every registered command, response, constant, and enumeration
// value must appear exactly once with its assigned id and its
// dictionary-key desc string; chunking and the extension_done
// range-end marker must follow the documented contract.
//
// The final test dumps a full enumeration transcript to
// build/extdesc_wire.bin for tools/test_extbind.py — the host-side
// reference binding is validated against these exact device bytes.

#include "intentproto/method.hpp"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int g_failures = 0;
#define CHECK(cond)                                                     \
    do {                                                                \
        if (!(cond)) {                                                  \
            printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);      \
            g_failures++;                                               \
        }                                                               \
    } while (0)

// ---------------- the device-specific slice ----------------

KLIPPER_CONSTANT(CLOCK_FREQ, 64000000);
KLIPPER_CONSTANT_STR(MCU, "extdesc-test");

KLIPPER_ENUMERATION(spi_bus, spi0, 0);
KLIPPER_ENUMERATION(spi_bus, spi1, 1);

KLIPPER_RESPONSE(oams_action_status,
                 (uint8_t, action), (uint8_t, code), (uint32_t, value));

static struct {
    int load_calls = 0;
    uint8_t spool = 0xff;
    int trim_calls = 0;
    int16_t trim = 0;
    int32_t bias = 0;
    int blob_calls = 0;
    uint32_t blob_len = 0;
} g_seen;

KLIPPER_METHOD(oams_cmd_load_spool, (uint8_t, spool)) {
    g_seen.load_calls++;
    g_seen.spool = spool;
    intentproto::reply(oams_action_status{0, 0, spool});
}

KLIPPER_METHOD(ext_cmd_trim, (int16_t, trim), (int32_t, bias)) {
    g_seen.trim_calls++;
    g_seen.trim = trim;
    g_seen.bias = bias;
}

KLIPPER_METHOD(ext_cmd_blob, (uint8_t, oid), (intentproto::buf, data)) {
    (void)oid;
    g_seen.blob_calls++;
    g_seen.blob_len = data.len;
}

// ---------------- test transport ----------------

static uint8_t g_tx_buf[8192];
static size_t g_tx_len = 0;

static int test_write(const uint8_t* data, size_t len, void*) {
    if (g_tx_len + len <= sizeof(g_tx_buf)) {
        memcpy(g_tx_buf + g_tx_len, data, len);
        g_tx_len += len;
    }
    return (int)len;
}

static void tx_reset() { g_tx_len = 0; }

// Split the captured tx stream into frames; returns frame count.
static int tx_frames(const uint8_t* frames[], int max) {
    int n = 0;
    size_t pos = 0;
    while (pos < g_tx_len && n < max) {
        uint8_t len = g_tx_buf[pos];
        if (len < intentproto::MESSAGE_MIN || pos + len > g_tx_len)
            break;
        frames[n++] = g_tx_buf + pos;
        pos += len;
    }
    return n;
}

// Host-side frame builder for driving the MCU-side library.
struct HostFrame {
    uint8_t buf[intentproto::MESSAGE_MAX];
    uint8_t* p;
    uint8_t seq;
    explicit HostFrame(uint8_t seq_) : p(buf + 2), seq(seq_) {}
    HostFrame& put_raw(const uint8_t* d, size_t n) {
        memcpy(p, d, n);
        p += n;
        return *this;
    }
    size_t finish() {
        size_t total = (size_t)(p - buf) + intentproto::TRAILER_SIZE;
        buf[0] = (uint8_t)total;
        buf[1] = (uint8_t)(intentproto::MESSAGE_DEST
                           | (seq & intentproto::MESSAGE_SEQ_MASK));
        uint16_t crc = intentproto::crc16_ccitt(buf, total - 3);
        buf[total - 3] = (uint8_t)(crc >> 8);
        buf[total - 2] = (uint8_t)(crc & 0xff);
        buf[total - 1] = intentproto::MESSAGE_SYNC;
        return total;
    }
};

// One command payload in, the resulting response payloads out (the
// trailing ack frame is dropped).
struct Payload {
    uint8_t data[intentproto::MESSAGE_MAX];
    size_t len;
};

static uint8_t g_seq = 0;

static int exchange(const uint8_t* cmd, size_t cmd_len,
                    Payload* out, int max) {
    tx_reset();
    HostFrame f(g_seq++);
    f.put_raw(cmd, cmd_len);
    intentproto::rx(f.buf, f.finish());
    const uint8_t* frames[64];
    int nf = tx_frames(frames, 64);
    int n = 0;
    for (int i = 0; i < nf; i++) {
        size_t plen = (size_t)frames[i][0] - intentproto::MESSAGE_MIN;
        if (!plen || n >= max)
            continue;
        memcpy(out[n].data, frames[i] + intentproto::HEADER_SIZE, plen);
        out[n].len = plen;
        n++;
    }
    return n;
}

static size_t make_list_cmd(uint8_t* out, uint32_t msgid, uint32_t start,
                            uint32_t count) {
    uint8_t* p = out;
    p = intentproto::vlq_encode(p, msgid);
    p = intentproto::vlq_encode(p, start);
    p = intentproto::vlq_encode(p, count);
    return (size_t)(p - out);
}

static uint32_t take_u32(const uint8_t** pp, const uint8_t* end) {
    uint32_t v = 0;
    CHECK(intentproto::vlq_decode(pp, end, &v));
    return v;
}

static const intentproto::Command* cmd_by_name(const char* name) {
    for (const intentproto::Command* c = intentproto::first_command(); c;
         c = c->next)
        if (!strcmp(c->name, name))
            return c;
    return nullptr;
}

static const intentproto::Response* res_by_name(const char* name) {
    for (const intentproto::Response* r = intentproto::first_response(); r;
         r = r->next)
        if (!strcmp(r->name, name))
            return r;
    return nullptr;
}

// ---------------- enumeration driver ----------------

struct Entry {
    uint32_t kind;
    uint32_t id;         // extension_desc only (0 for constant_desc)
    char desc[96];
};

// Parse one enumeration response payload into *e; returns 0 for an
// entry, 1 for extension_done (total in *total), -1 for junk.
static int parse_entry(const Payload& p, bool with_id, uint32_t desc_id,
                       uint32_t done_id, Entry* e, uint32_t* total) {
    const uint8_t* q = p.data;
    const uint8_t* end = p.data + p.len;
    uint32_t msgid = take_u32(&q, end);
    if (msgid == done_id) {
        *total = take_u32(&q, end);
        CHECK(q == end);
        return 1;
    }
    if (msgid != desc_id)
        return -1;
    e->kind = take_u32(&q, end);
    e->id = with_id ? take_u32(&q, end) : 0;
    uint32_t n = take_u32(&q, end);
    CHECK(n == (uint32_t)(end - q));
    CHECK(n < sizeof(e->desc));
    memcpy(e->desc, q, n);
    e->desc[n] = '\0';
    return 0;
}

// Drive a full paginated enumeration; asserts each call returns at
// most `chunk` entries and that extension_done arrives exactly once,
// after the last entry. Returns the entry count; *total gets the
// device-reported total.
static int enumerate_all(uint32_t list_id, uint32_t desc_id,
                         uint32_t done_id, bool with_id, uint32_t chunk,
                         Entry* out, int max, uint32_t* total) {
    int n = 0;
    *total = 0;
    for (uint32_t start = 0;; start += chunk) {
        uint8_t cmd[16];
        size_t cl = make_list_cmd(cmd, list_id, start, chunk);
        Payload resp[16];
        int nr = exchange(cmd, cl, resp, 16);
        bool done = false;
        int got = 0;
        for (int i = 0; i < nr; i++) {
            Entry e;
            int r = parse_entry(resp[i], with_id, desc_id, done_id, &e,
                                total);
            CHECK(r >= 0);
            if (r == 1) {
                CHECK(i == nr - 1);   // done is the range's last frame
                done = true;
            } else if (r == 0 && n < max) {
                out[n++] = e;
                got++;
            }
        }
        CHECK((uint32_t)got <= chunk);
        if (done)
            return n;
        CHECK(got == (int)chunk);     // no done => a full chunk arrived
        CHECK(start < 1000);          // runaway guard
    }
}

static const Entry* entry_by_desc_prefix(const Entry* entries, int n,
                                         const char* name) {
    const Entry* found = nullptr;
    size_t len = strlen(name);
    int hits = 0;
    for (int i = 0; i < n; i++)
        if (!strncmp(entries[i].desc, name, len)
            && (entries[i].desc[len] == ' ' || entries[i].desc[len] == '\0'
                || entries[i].desc[len] == '=')) {
            found = &entries[i];
            hits++;
        }
    CHECK(hits == 1);   // exactly once
    return found;
}

// ---------------- tests ----------------

static uint32_t g_list_ext, g_list_const, g_ext_desc, g_const_desc,
    g_ext_done;

static void lookup_meta_ids() {
    const intentproto::Command* le = cmd_by_name("list_extensions");
    const intentproto::Command* lc = cmd_by_name("list_constants");
    const intentproto::Response* ed = res_by_name("extension_desc");
    const intentproto::Response* cd = res_by_name("constant_desc");
    const intentproto::Response* dn = res_by_name("extension_done");
    CHECK(le && lc && ed && cd && dn);
    if (!(le && lc && ed && cd && dn))
        exit(1);
    g_list_ext = le->id;
    g_list_const = lc->id;
    g_ext_desc = ed->id;
    g_const_desc = cd->id;
    g_ext_done = dn->id;
}

static void test_list_extensions_complete() {
    Entry entries[64];
    uint32_t total = 0;
    int n = enumerate_all(g_list_ext, g_ext_desc, g_ext_done,
                          /*with_id=*/true, 8, entries, 64, &total);

    // The device-reported total covers the whole registry.
    uint32_t expect = 0;
    for (const intentproto::Command* c = intentproto::first_command(); c;
         c = c->next, expect++) {}
    for (const intentproto::Response* r = intentproto::first_response(); r;
         r = r->next, expect++) {}
    CHECK(total == expect);
    CHECK((uint32_t)n == total);

    // Every registered command and response appears exactly once,
    // with its assigned id and its dictionary-key desc string.
    char key[96];
    for (const intentproto::Command* c = intentproto::first_command(); c;
         c = c->next) {
        const Entry* e = entry_by_desc_prefix(entries, n, c->name);
        CHECK(e != nullptr);
        if (!e)
            continue;
        CHECK(e->kind == intentproto::EXTDESC_KIND_COMMAND);
        CHECK(e->id == c->id);
        CHECK(intentproto::message_key(key, sizeof(key), c->name,
                                       c->param_names, c->param_types,
                                       c->num_params));
        CHECK(!strcmp(e->desc, key));
    }
    for (const intentproto::Response* r = intentproto::first_response(); r;
         r = r->next) {
        const Entry* e = entry_by_desc_prefix(entries, n, r->name);
        CHECK(e != nullptr);
        if (!e)
            continue;
        CHECK(e->kind == intentproto::EXTDESC_KIND_RESPONSE);
        CHECK(e->id == r->id);
        CHECK(intentproto::message_key(key, sizeof(key), r->name,
                                       r->field_names, r->field_types,
                                       r->num_fields));
        CHECK(!strcmp(e->desc, key));
    }

    // Literal spot checks (the wire strings, not the builder).
    const Entry* e = entry_by_desc_prefix(entries, n, "oams_cmd_load_spool");
    CHECK(e && !strcmp(e->desc, "oams_cmd_load_spool spool=%c"));
    e = entry_by_desc_prefix(entries, n, "ext_cmd_trim");
    CHECK(e && !strcmp(e->desc, "ext_cmd_trim trim=%hi bias=%i"));
    e = entry_by_desc_prefix(entries, n, "ext_cmd_blob");
    CHECK(e && !strcmp(e->desc, "ext_cmd_blob oid=%c data=%.*s"));
    // Self-describing all the way down: the meta-messages are in the
    // stream themselves.
    e = entry_by_desc_prefix(entries, n, "list_extensions");
    CHECK(e && !strcmp(e->desc, "list_extensions start=%u count=%c"));
    CHECK(e && e->id == g_list_ext);
    e = entry_by_desc_prefix(entries, n, "extension_desc");
    CHECK(e && !strcmp(e->desc, "extension_desc kind=%c id=%u desc=%.*s"));
    e = entry_by_desc_prefix(entries, n, "extension_done");
    CHECK(e && !strcmp(e->desc, "extension_done total=%u"));
}

static void test_chunking() {
    // A chunk size of 3 forces pagination; enumerate_all() asserts
    // per-call bounds and done placement internally.
    Entry entries[64];
    uint32_t total3 = 0, total8 = 0;
    int n3 = enumerate_all(g_list_ext, g_ext_desc, g_ext_done, true, 3,
                           entries, 64, &total3);
    int n8 = enumerate_all(g_list_ext, g_ext_desc, g_ext_done, true, 8,
                           entries, 64, &total8);
    CHECK(n3 == n8 && total3 == total8);

    // start past the end: an immediate lone extension_done.
    uint8_t cmd[16];
    size_t cl = make_list_cmd(cmd, g_list_ext, 1000, 8);
    Payload resp[16];
    int nr = exchange(cmd, cl, resp, 16);
    CHECK(nr == 1);
    Entry e;
    uint32_t total = 0;
    CHECK(parse_entry(resp[0], true, g_ext_desc, g_ext_done, &e, &total)
          == 1);
    CHECK(total == total8);

    // count is clamped to EXTDESC_COUNT_MAX per call.
    cl = make_list_cmd(cmd, g_list_ext, 0, 50);
    nr = exchange(cmd, cl, resp, 16);
    int got = 0;
    for (int i = 0; i < nr; i++)
        if (parse_entry(resp[i], true, g_ext_desc, g_ext_done, &e, &total)
            == 0)
            got++;
    CHECK((uint32_t)got <= intentproto::EXTDESC_COUNT_MAX);
}

static void test_list_constants_complete() {
    Entry entries[64];
    uint32_t total = 0;
    int n = enumerate_all(g_list_const, g_const_desc, g_ext_done,
                          /*with_id=*/false, 8, entries, 64, &total);

    uint32_t expect = 0;
    for (const intentproto::Constant* k = intentproto::first_constant(); k;
         k = k->next, expect++) {}
    for (const intentproto::Enumeration* e =
             intentproto::first_enumeration(); e; e = e->next, expect++) {}
    CHECK(total == expect);
    CHECK((uint32_t)n == total);

    // Every constant and enumeration value, exactly once, with the
    // documented text encoding.
    char key[96];
    for (const intentproto::Constant* k = intentproto::first_constant(); k;
         k = k->next) {
        const Entry* e = entry_by_desc_prefix(entries, n, k->name);
        CHECK(e != nullptr);
        if (!e)
            continue;
        CHECK(e->kind == (k->str_value ? intentproto::CONSTDESC_KIND_STR
                                       : intentproto::CONSTDESC_KIND_INT));
        CHECK(intentproto::constant_desc(key, sizeof(key), *k));
        CHECK(!strcmp(e->desc, key));
    }
    int enum_hits = 0;
    for (const intentproto::Enumeration* en =
             intentproto::first_enumeration(); en; en = en->next) {
        CHECK(intentproto::enumeration_desc(key, sizeof(key), *en));
        int hits = 0;
        for (int i = 0; i < n; i++)
            if (!strcmp(entries[i].desc, key)) {
                CHECK(entries[i].kind == intentproto::CONSTDESC_KIND_ENUM);
                hits++;
            }
        CHECK(hits == 1);
        enum_hits++;
    }
    CHECK(enum_hits == 2);

    // Literal spot checks, including the library-owned constant.
    const Entry* e = entry_by_desc_prefix(entries, n, "FRAMING_V2");
    CHECK(e && !strcmp(e->desc, "FRAMING_V2=1")
          && e->kind == intentproto::CONSTDESC_KIND_INT);
    e = entry_by_desc_prefix(entries, n, "MCU");
    CHECK(e && !strcmp(e->desc, "MCU=extdesc-test")
          && e->kind == intentproto::CONSTDESC_KIND_STR);
    e = entry_by_desc_prefix(entries, n, "spi_bus.spi0");
    CHECK(e && !strcmp(e->desc, "spi_bus.spi0=0")
          && e->kind == intentproto::CONSTDESC_KIND_ENUM);
}

static void test_dictionary_contains_meta() {
    // The meta-messages carry legacy-assigned ids and land in the
    // legacy dictionary like any other registered message.
    static char json[8192];
    size_t n = intentproto::build_dictionary(json, sizeof(json));
    CHECK(n > 0);
    char key[128];
    snprintf(key, sizeof(key),
             "\"list_extensions start=%%u count=%%c\":%u", g_list_ext);
    CHECK(strstr(json, key) != nullptr);
    snprintf(key, sizeof(key),
             "\"extension_desc kind=%%c id=%%u desc=%%.*s\":%u",
             g_ext_desc);
    CHECK(strstr(json, key) != nullptr);
    snprintf(key, sizeof(key), "\"extension_done total=%%u\":%u",
             g_ext_done);
    CHECK(strstr(json, key) != nullptr);
}

// ---------------- wire transcript for tools/test_extbind.py ----------------

static void put_hex(FILE* fp, const uint8_t* d, size_t n) {
    for (size_t i = 0; i < n; i++)
        fprintf(fp, "%02x", d[i]);
}

// Replay one enumeration into the transcript: `cmd` lines carry the
// exact command payload a binding host must produce, `rsp` lines the
// device's response payloads.
static void dump_enumeration(FILE* fp, uint32_t list_id, uint32_t done_id) {
    for (uint32_t start = 0;; start += 8) {
        uint8_t cmd[16];
        size_t cl = make_list_cmd(cmd, list_id, start, 8);
        Payload resp[16];
        int nr = exchange(cmd, cl, resp, 16);
        fprintf(fp, "cmd ");
        put_hex(fp, cmd, cl);
        fprintf(fp, "\n");
        bool done = false;
        for (int i = 0; i < nr; i++) {
            fprintf(fp, "rsp ");
            put_hex(fp, resp[i].data, resp[i].len);
            fprintf(fp, "\n");
            const uint8_t* q = resp[i].data;
            const uint8_t* end = q + resp[i].len;
            if (take_u32(&q, end) == done_id)
                done = true;
        }
        if (done)
            return;
        CHECK(start < 1000);
    }
}

static void test_write_transcript() {
    FILE* fp = fopen("build/extdesc_wire.bin", "w");
    CHECK(fp != nullptr);
    if (!fp)
        return;
    fprintf(fp, "# intentproto extension self-description transcript\n");
    fprintf(fp, "# written by tests/test_extdesc.cpp; read by"
                " tools/test_extbind.py\n");
    fprintf(fp,
            "meta list_extensions=%u list_constants=%u extension_desc=%u"
            " constant_desc=%u extension_done=%u\n",
            g_list_ext, g_list_const, g_ext_desc, g_const_desc, g_ext_done);
    dump_enumeration(fp, g_list_ext, g_ext_done);
    dump_enumeration(fp, g_list_const, g_ext_done);

    // Round-trip anchors: payloads built here, dispatched through
    // the device, and asserted against the handlers — the python
    // binding must encode byte-identical payloads.
    uint8_t cmd[32];
    uint8_t* p = cmd;
    p = intentproto::vlq_encode(p, cmd_by_name("oams_cmd_load_spool")->id);
    p = intentproto::vlq_encode(p, 3);
    Payload resp[4];
    int nr = exchange(cmd, (size_t)(p - cmd), resp, 4);
    CHECK(g_seen.load_calls == 1 && g_seen.spool == 3);
    CHECK(nr == 1);   // the oams_action_status reply
    fprintf(fp, "enc load_spool ");
    put_hex(fp, cmd, (size_t)(p - cmd));
    fprintf(fp, "\n");
    if (nr == 1) {
        fprintf(fp, "par action_status ");
        put_hex(fp, resp[0].data, resp[0].len);
        fprintf(fp, "\n");
    }

    p = cmd;
    p = intentproto::vlq_encode(p, cmd_by_name("ext_cmd_trim")->id);
    p = intentproto::vlq_encode(p, (uint32_t)(int32_t)-123);
    p = intentproto::vlq_encode(p, (uint32_t)(int32_t)-70000);
    exchange(cmd, (size_t)(p - cmd), resp, 4);
    CHECK(g_seen.trim_calls == 1 && g_seen.trim == -123
          && g_seen.bias == -70000);
    fprintf(fp, "enc trim ");
    put_hex(fp, cmd, (size_t)(p - cmd));
    fprintf(fp, "\n");

    static const uint8_t blob[] = {0xde, 0xad, 0x7e, 0x00};
    p = cmd;
    p = intentproto::vlq_encode(p, cmd_by_name("ext_cmd_blob")->id);
    p = intentproto::vlq_encode(p, 9);
    p = intentproto::vlq_encode(p, sizeof(blob));
    memcpy(p, blob, sizeof(blob));
    p += sizeof(blob);
    exchange(cmd, (size_t)(p - cmd), resp, 4);
    CHECK(g_seen.blob_calls == 1 && g_seen.blob_len == sizeof(blob));
    fprintf(fp, "enc blob ");
    put_hex(fp, cmd, (size_t)(p - cmd));
    fprintf(fp, "\n");

    fclose(fp);
}

int main() {
    intentproto::Config cfg;
    cfg.write = test_write;
    cfg.version = "extdesc-1.0";
    cfg.build_version = "extdesc-build";
    intentproto::init(cfg);

    lookup_meta_ids();
    test_list_extensions_complete();
    test_chunking();
    test_list_constants_complete();
    test_dictionary_contains_meta();
    test_write_transcript();

    if (g_failures) {
        printf("%d FAILURE(S)\n", g_failures);
        return 1;
    }
    printf("all tests passed\n");
    return 0;
}
