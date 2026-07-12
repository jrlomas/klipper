#ifndef INTENTPROTO_PROTO_HPP
#define INTENTPROTO_PROTO_HPP
// intentproto — the single protocol library of the motion-intentions
// fork (RFC 0001, doc 10).
//
// Core profile: freestanding C++ — no heap, no exceptions, no RTTI,
// no virtual dispatch, no STL containers. Bytes in, bytes out,
// caller-owned buffers; the caller owns timers and all I/O.
//
// This file is the plain-data core: wire constants, codecs, the
// descriptor records, and the link/session entry points. The
// annotation-style declaration layer (KLIPPER_METHOD / …) lives in
// method.hpp and produces the descriptor records defined here.

#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace intentproto {

// ---- legacy wire framing limits (see Klipper docs/Protocol.md) ----
constexpr size_t MESSAGE_MAX = 64;
constexpr size_t MESSAGE_MIN = 5;   // len + seq + crc16 + sync
constexpr size_t HEADER_SIZE = 2;   // len, seq
constexpr size_t TRAILER_SIZE = 3;  // crc16 (2), sync (1)
constexpr size_t PAYLOAD_MAX = MESSAGE_MAX - MESSAGE_MIN;
constexpr uint8_t MESSAGE_DEST = 0x10;
constexpr uint8_t MESSAGE_SEQ_MASK = 0x0f;
constexpr uint8_t MESSAGE_SYNC = 0x7e;

// Message ids fixed by the legacy protocol.
constexpr uint32_t MSGID_IDENTIFY_RESPONSE = 0;
constexpr uint32_t MSGID_IDENTIFY = 1;
// First id available for registered commands/responses.
constexpr uint32_t MSGID_FIRST_FREE = 2;

// ---- parameter kinds carried by wire messages ----
enum class ParamType : uint8_t { U8, I8, U16, I16, U32, I32, Bool, Buf };

// A length-delimited byte buffer parameter (the legacy "%.*s" wire
// type): encoded as a VLQ length followed by that many raw bytes.
// Inside a command handler the data pointer aliases the receive
// frame buffer and is only valid for the duration of the call — copy
// the bytes out to keep them.
struct buf {
    const uint8_t* data;
    uint32_t len;
};

// Dictionary format specifier for a parameter type ("%c", "%hi", ...).
const char* format_of(ParamType t);

// ---- codecs ----
// CRC-16/CCITT-FALSE (poly 0x1021, init 0xffff, MSB first).
uint16_t crc16_ccitt(const uint8_t* buf, size_t len);

// Variable length quantity codec (7 bits per byte, MSB continuation,
// leading group sign-extended — the legacy protocol's integer format).
// vlq_encode writes at most 5 bytes and returns the advanced pointer.
uint8_t* vlq_encode(uint8_t* out, uint32_t v);
// Bounded decode; returns false on truncated input (pointer unchanged).
bool vlq_decode(const uint8_t** pp, const uint8_t* end, uint32_t* out);

// ---- descriptor records ----
// These are plain static data, created by the declaration macros in
// method.hpp (or by hand in plain C++). Registration is an intrusive,
// heap-free linked list built by static initialization; init() freezes
// it and assigns wire ids in definition order.

// Decoded arguments reach handler trampolines as an ArgWord array.
// Convention: an integer/bool parameter occupies one word holding its
// sign-extended 32-bit value; a buf parameter occupies TWO
// consecutive words — the length, then the data pointer cast through
// uintptr_t (which is why ArgWord is pointer-sized).
using ArgWord = uintptr_t;

struct Command {
    const char* name;
    const char* const* param_names;   // num_params entries
    const ParamType* param_types;     // num_params entries
    uint8_t num_params;
    void (*invoke)(const ArgWord* args);
    uint16_t id;                      // assigned by init()
    Command* next;
    Command(const char* name_, const char* const* pnames,
            const ParamType* ptypes, uint8_t nparams,
            void (*fn)(const ArgWord*));
};

struct Writer {
    uint8_t* base;
    uint8_t* p;
    uint8_t* end;
    bool overflow;

    Writer(uint8_t* buf, size_t cap)
        : base(buf), p(buf), end(buf + cap), overflow(false) {}
    void put_u32(uint32_t v) {
        if (p + 5 > end) { overflow = true; return; }
        p = vlq_encode(p, v);
    }
    void put(uint8_t v)  { put_u32(v); }
    void put(uint16_t v) { put_u32(v); }
    void put(uint32_t v) { put_u32(v); }
    void put(int8_t v)   { put_u32((uint32_t)(int32_t)v); }
    void put(int16_t v)  { put_u32((uint32_t)(int32_t)v); }
    void put(int32_t v)  { put_u32((uint32_t)v); }
    void put(bool v)     { put_u32(v ? 1u : 0u); }
    void put(buf v)      { put_bytes(v.data, v.len); }
    void put_bytes(const uint8_t* d, uint32_t n) {
        put_u32(n);
        if (p + n > end) { overflow = true; return; }
        memcpy(p, d, n);
        p += n;
    }
    size_t size() const { return (size_t)(p - base); }
};

struct Response {
    const char* name;
    const char* const* field_names;
    const ParamType* field_types;
    uint8_t num_fields;
    void (*pack)(Writer& w, const void* value);
    uint16_t id;                      // assigned by init()
    Response* next;
    Response(const char* name_, const char* const* fnames,
             const ParamType* ftypes, uint8_t nfields,
             void (*pack_)(Writer&, const void*));
};

struct Constant {
    const char* name;
    const char* str_value;            // nullptr => integer constant
    int32_t int_value;
    Constant* next;
    Constant(const char* n, int32_t v);
    Constant(const char* n, const char* v);
};

// One named value of a dictionary enumeration. The dictionary
// builder groups consecutive records sharing enum_name into one
// "enumerations" object, so declare all values of an enumeration
// together (definition order is preserved by init()).
struct Enumeration {
    const char* enum_name;
    const char* value_name;
    int32_t value;
    Enumeration* next;
    Enumeration(const char* en, const char* vn, int32_t v);
};

// ---- link/session ----
struct Config {
    // Transport transmit hook: must write len bytes (a whole frame).
    int (*write)(const uint8_t* data, size_t len, void* user);
    void* user;
    // Identify payload served in chunks to `identify` requests. For a
    // legacy klippy host this must be the zlib-compressed dictionary
    // (compress the build_dictionary() output at build time).
    const uint8_t* identify_blob;
    size_t identify_blob_len;
    // Strings reported in the dictionary.
    const char* version;
    const char* build_version;

    Config()
        : write(nullptr), user(nullptr), identify_blob(nullptr),
          identify_blob_len(0), version("intentproto-dev"),
          build_version("") {}
};

// Freeze registration, assign wire ids (definition order, commands
// first then responses, starting at MSGID_FIRST_FREE), reset link
// state. Call once after static initialization, before rx().
void init(const Config& cfg);

// Feed raw link bytes in any chunking. Both framings are accepted at
// all times: legacy frames are CRC checked; frames whose seq byte
// sets FRAME_V2_FLAG are BCH decoded (framing v2, RFC 0001 doc 07).
// Valid frames are acked, damaged ones nacked, and their messages
// dispatched to registered command handlers from inside this call.
// `identify` is handled by the library.
//
// Negotiation, device side: the first VALID v2 frame latches the
// link to framing v2 — every transmit (acks, naks, responses,
// identify) switches to the BCH trailer and stays there; the link
// never auto-downgrades (only init() resets it). The capability is
// advertised as the dictionary constant FRAMING_V2=1, registered by
// init() itself.
void rx(const uint8_t* data, size_t len);

// True once the link has latched to framing v2 (see rx() above).
bool link_framing_v2();

// The configuration passed to the most recent init().
const Config& current_config();

// Registry access (dictionary builder, tests, tooling).
const Command* first_command();
const Response* first_response();
const Constant* first_constant();
const Enumeration* first_enumeration();
const Command* find_command(uint32_t id);

// Counters for link diagnostics.
struct LinkStats {
    uint32_t frames_ok;
    uint32_t crc_errors;
    uint32_t framing_errors;
    uint32_t unknown_msgids;
    uint32_t bch_errors;      // uncorrectable v2 frames (nacked)
    uint32_t bch_corrected;   // bit errors repaired in accepted frames
};
const LinkStats& link_stats();

// Emit the (uncompressed) legacy dictionary JSON for the current
// registry into out; returns the length, or 0 if cap was too small.
// This is data->data serialization of the registered descriptors —
// nothing scrapes source code. Production builds run this in a host
// tool and zlib-compress the result into Config::identify_blob.
size_t build_dictionary(char* out, size_t cap);

namespace detail {
void send_response(const Response& r, const void* value);
} // namespace detail

// Send a response struct declared with KLIPPER_RESPONSE. The
// descriptor is found through the _ip_desc_of() shim the macro
// declares next to the struct (resolved by argument-dependent
// lookup, so it works from any namespace in the declaring TU).
template <typename T>
inline void reply(const T& v) {
    detail::send_response(_ip_desc_of(static_cast<const T*>(nullptr)), &v);
}

} // namespace intentproto

#endif // INTENTPROTO_PROTO_HPP
