// intentproto core: codecs, registry, framing, dispatch.
// Freestanding profile: no heap, no exceptions, no RTTI; the only
// libc dependencies are memcpy-class functions.

#include "intentproto/proto.hpp"

namespace intentproto {

// ---------------- codecs ----------------

const char* format_of(ParamType t) {
    switch (t) {
    case ParamType::U8:   return "%c";
    case ParamType::I8:   return "%c";
    case ParamType::U16:  return "%hu";
    case ParamType::I16:  return "%hi";
    case ParamType::U32:  return "%u";
    case ParamType::I32:  return "%i";
    case ParamType::Bool: return "%c";
    case ParamType::Buf:  return "%.*s";
    }
    return "%u";
}

// Reflected CRC-16/MCRF4XX (poly 0x8408, init 0xffff): the variant
// the legacy Klipper wire actually uses. Check("123456789") = 0x6f91.
uint16_t crc16_ccitt(const uint8_t* buf, size_t len) {
    uint16_t crc = 0xffff;
    while (len--) {
        crc ^= *buf++;
        for (int i = 0; i < 8; i++)
            crc = (crc & 1) ? (uint16_t)((crc >> 1) ^ 0x8408)
                            : (uint16_t)(crc >> 1);
    }
    return crc;
}

// The legacy VLQ: 7 data bits per byte, MSB set on all but the last
// byte, most significant group first. Decoders sign-extend when the
// leading group's bits 5-6 are both set, so the encoded length must
// be the shortest one whose leading group round-trips the sign:
// one byte covers [-2^5, 3*2^5), two cover [-2^12, 3*2^12), etc.
uint8_t* vlq_encode(uint8_t* out, uint32_t v) {
    int32_t sv = (int32_t)v;
    int len;
    if (sv >= -(1 << 5) && sv < (3 << 5))
        len = 1;
    else if (sv >= -(1 << 12) && sv < (3 << 12))
        len = 2;
    else if (sv >= -(1 << 19) && sv < (3 << 19))
        len = 3;
    else if (sv >= -(1L << 26) && sv < (3L << 26))
        len = 4;
    else
        len = 5;
    for (int i = len - 1; i > 0; i--)
        *out++ = (uint8_t)(((v >> (7 * i)) & 0x7f) | 0x80);
    *out++ = (uint8_t)(v & 0x7f);
    return out;
}

bool vlq_decode(const uint8_t** pp, const uint8_t* end, uint32_t* out) {
    const uint8_t* p = *pp;
    if (p >= end)
        return false;
    uint8_t c = *p++;
    uint32_t v = c & 0x7f;
    if ((c & 0x60) == 0x60)
        v |= (uint32_t)-0x20;   // sign-extend a negative leading group
    int guard = 4;              // at most 5 bytes total
    while (c & 0x80) {
        if (p >= end || --guard < 0)
            return false;
        c = *p++;
        v = (v << 7) | (c & 0x7f);
    }
    *pp = p;
    *out = v;
    return true;
}

// ---------------- registry ----------------

namespace {
Command* g_commands = nullptr;
Response* g_responses = nullptr;
Constant* g_constants = nullptr;
Enumeration* g_enumerations = nullptr;
bool g_finalized = false;
} // namespace

Command::Command(const char* name_, const char* const* pnames,
                 const ParamType* ptypes, uint8_t nparams,
                 void (*fn)(const ArgWord*))
    : name(name_), param_names(pnames), param_types(ptypes),
      num_params(nparams), invoke(fn), id(0), next(g_commands) {
    g_commands = this;
}

Response::Response(const char* name_, const char* const* fnames,
                   const ParamType* ftypes, uint8_t nfields,
                   void (*pack_)(Writer&, const void*))
    : name(name_), field_names(fnames), field_types(ftypes),
      num_fields(nfields), pack(pack_), id(0), next(g_responses) {
    g_responses = this;
}

Constant::Constant(const char* n, int32_t v)
    : name(n), str_value(nullptr), int_value(v), next(g_constants) {
    g_constants = this;
}

Constant::Constant(const char* n, const char* v)
    : name(n), str_value(v), int_value(0), next(g_constants) {
    g_constants = this;
}

Enumeration::Enumeration(const char* en, const char* vn, int32_t v)
    : enum_name(en), value_name(vn), value(v), next(g_enumerations) {
    g_enumerations = this;
}

namespace {

// Static initialization builds the lists head-first, i.e. in reverse
// definition order; put them back so ids follow the source.
template <typename T>
T* reverse_list(T* head) {
    T* prev = nullptr;
    while (head) {
        T* next = head->next;
        head->next = prev;
        prev = head;
        head = next;
    }
    return prev;
}

} // namespace

const Command* first_command() { return g_commands; }
const Response* first_response() { return g_responses; }
const Constant* first_constant() { return g_constants; }
const Enumeration* first_enumeration() { return g_enumerations; }

const Command* find_command(uint32_t id) {
    for (const Command* c = g_commands; c; c = c->next)
        if (c->id == id)
            return c;
    return nullptr;
}

// ---------------- link state ----------------

namespace {

struct Link {
    Config cfg;
    uint8_t last_rx_seq;
    LinkStats stats;
    // frame receive state machine
    enum class RxState : uint8_t { Sync, Length, Body } state;
    uint8_t buf[MESSAGE_MAX];
    size_t pos;
};

Link g_link;

void tx(const uint8_t* data, size_t len) {
    if (g_link.cfg.write)
        g_link.cfg.write(data, len, g_link.cfg.user);
}

uint8_t reply_seq_byte() {
    return (uint8_t)(((g_link.last_rx_seq + 1) & MESSAGE_SEQ_MASK)
                     | MESSAGE_DEST);
}

// Wrap a payload already placed at frame+HEADER_SIZE and transmit.
void tx_frame(uint8_t* frame, size_t payload_len, uint8_t seq_byte) {
    size_t total = payload_len + MESSAGE_MIN;
    frame[0] = (uint8_t)total;
    frame[1] = seq_byte;
    uint16_t crc = crc16_ccitt(frame, total - TRAILER_SIZE);
    frame[total - 3] = (uint8_t)(crc >> 8);
    frame[total - 2] = (uint8_t)(crc & 0xff);
    frame[total - 1] = MESSAGE_SYNC;
    tx(frame, total);
}

void send_ack() {
    uint8_t frame[MESSAGE_MIN];
    tx_frame(frame, 0, reply_seq_byte());
}

void send_nack() {
    uint8_t frame[MESSAGE_MIN];
    uint8_t seq = (uint8_t)(((g_link.last_rx_seq - 1) & MESSAGE_SEQ_MASK)
                            | MESSAGE_DEST);
    tx_frame(frame, 0, seq);
}

// The identify command (msgid 1) is core protocol, owned by the
// library: serve identify_blob in chunks as identify_response (0).
void handle_identify(uint32_t offset, uint32_t count) {
    const uint8_t* blob = g_link.cfg.identify_blob;
    size_t blob_len = g_link.cfg.identify_blob_len;
    uint32_t n = 0;
    if (blob && offset < blob_len) {
        n = (uint32_t)(blob_len - offset);
        if (n > count)
            n = count;
        if (n > PAYLOAD_MAX - 10)   // msgid + offset vlq + length byte
            n = PAYLOAD_MAX - 10;
    }
    uint8_t frame[MESSAGE_MAX];
    Writer w(frame + HEADER_SIZE, PAYLOAD_MAX);
    w.put_u32(MSGID_IDENTIFY_RESPONSE);
    w.put_u32(offset);
    w.put_bytes(blob ? blob + offset : (const uint8_t*)"", n);
    if (!w.overflow)
        tx_frame(frame, w.size(), reply_seq_byte());
}

constexpr int MAX_ARG_WORDS = 16;

// Dispatch one message from a block; returns false if the block must
// be abandoned (unknown id / malformed args - without the message's
// descriptor the remaining bytes cannot be delimited).
bool dispatch_one(const uint8_t** pp, const uint8_t* end) {
    uint32_t msgid;
    if (!vlq_decode(pp, end, &msgid))
        return false;
    if (msgid == MSGID_IDENTIFY) {
        uint32_t offset, count;
        if (!vlq_decode(pp, end, &offset) || !vlq_decode(pp, end, &count))
            return false;
        handle_identify(offset, count);
        return true;
    }
    const Command* cmd = find_command(msgid);
    if (!cmd) {
        g_link.stats.unknown_msgids++;
        return false;
    }
    // Integers take one ArgWord; a buf takes two (length, pointer) —
    // see the ArgWord convention in proto.hpp.
    ArgWord args[MAX_ARG_WORDS];
    int w = 0;
    for (uint8_t i = 0; i < cmd->num_params; i++) {
        uint32_t v;
        if (!vlq_decode(pp, end, &v))
            return false;
        if (cmd->param_types[i] == ParamType::Buf) {
            // v is the length prefix; the raw bytes follow in place.
            if (w + 2 > MAX_ARG_WORDS || v > (uint32_t)(end - *pp))
                return false;
            args[w++] = v;
            args[w++] = (ArgWord)(uintptr_t)*pp;
            *pp += v;
        } else {
            if (w + 1 > MAX_ARG_WORDS)
                return false;
            args[w++] = v;
        }
    }
    cmd->invoke(args);
    return true;
}

void process_block(const uint8_t* frame, size_t total) {
    g_link.last_rx_seq = frame[1] & MESSAGE_SEQ_MASK;
    const uint8_t* p = frame + HEADER_SIZE;
    const uint8_t* end = frame + total - TRAILER_SIZE;
    while (p < end)
        if (!dispatch_one(&p, end))
            break;
    g_link.stats.frames_ok++;
    send_ack();
}

} // namespace

void init(const Config& cfg) {
    if (!g_finalized) {
        g_commands = reverse_list(g_commands);
        g_responses = reverse_list(g_responses);
        g_constants = reverse_list(g_constants);
        g_enumerations = reverse_list(g_enumerations);
        uint16_t id = MSGID_FIRST_FREE;
        for (Command* c = g_commands; c; c = c->next)
            c->id = id++;
        for (Response* r = g_responses; r; r = r->next)
            r->id = id++;
        g_finalized = true;
    }
    g_link.cfg = cfg;
    g_link.last_rx_seq = 0;
    g_link.stats = LinkStats{};
    // The sync byte is a frame *trailer*; a fresh link starts ready
    // to accept a length byte. Garbage resyncs via the error path.
    g_link.state = Link::RxState::Length;
    g_link.pos = 0;
}

void rx(const uint8_t* data, size_t len) {
    while (len--) {
        uint8_t byte = *data++;
        switch (g_link.state) {
        case Link::RxState::Sync:
            if (byte == MESSAGE_SYNC)
                g_link.state = Link::RxState::Length;
            break;
        case Link::RxState::Length:
            if (byte == MESSAGE_SYNC)
                break;                      // idle sync bytes between frames
            if (byte < MESSAGE_MIN || byte > MESSAGE_MAX) {
                g_link.stats.framing_errors++;
                g_link.state = Link::RxState::Sync;
                break;
            }
            g_link.buf[0] = byte;
            g_link.pos = 1;
            g_link.state = Link::RxState::Body;
            break;
        case Link::RxState::Body: {
            g_link.buf[g_link.pos++] = byte;
            size_t total = g_link.buf[0];
            if (g_link.pos < total)
                break;
            // complete frame: check trailer
            if (g_link.buf[total - 1] != MESSAGE_SYNC) {
                g_link.stats.framing_errors++;
                g_link.state = Link::RxState::Sync;
            } else {
                uint16_t want = (uint16_t)((g_link.buf[total - 3] << 8)
                                           | g_link.buf[total - 2]);
                uint16_t got = crc16_ccitt(g_link.buf, total - TRAILER_SIZE);
                if (want != got) {
                    g_link.stats.crc_errors++;
                    g_link.last_rx_seq = g_link.buf[1] & MESSAGE_SEQ_MASK;
                    send_nack();
                } else {
                    process_block(g_link.buf, total);
                }
                g_link.state = Link::RxState::Length;
            }
            g_link.pos = 0;
            break;
        }
        }
    }
}

const LinkStats& link_stats() { return g_link.stats; }

const Config& current_config() { return g_link.cfg; }

namespace detail {

void send_response(const Response& r, const void* value) {
    uint8_t frame[MESSAGE_MAX];
    Writer w(frame + HEADER_SIZE, PAYLOAD_MAX);
    w.put_u32(r.id);
    r.pack(w, value);
    if (!w.overflow)
        tx_frame(frame, w.size(), reply_seq_byte());
}

} // namespace detail

} // namespace intentproto
