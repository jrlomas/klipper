// Optional session-security layer: PSK-authenticated handshake, HKDF
// traffic-key schedule, epoch key rotation, and a sliding replay
// window over session-protected datagrams. Pure state machine — the
// caller owns entropy, timers and I/O. See session_sec.hpp for the
// scope decision (auth-only, not full DTLS) and message flow.

#include "intentproto/session_sec.hpp"

#include <string.h>

namespace intentproto {

namespace {

// HKDF-Expand context labels. The direction label plus the epoch
// number make each traffic key independent across direction and
// rotation; the finished label separates the handshake proof key.
const uint8_t LBL_C2S[] = "intentproto c2s v1";
const uint8_t LBL_S2C[] = "intentproto s2c v1";
const uint8_t LBL_FIN[] = "intentproto finished v1";

void store_be32(uint8_t* p, uint32_t v) {
    p[0] = (uint8_t)(v >> 24);
    p[1] = (uint8_t)(v >> 16);
    p[2] = (uint8_t)(v >> 8);
    p[3] = (uint8_t)v;
}

uint32_t load_be32(const uint8_t* p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16)
         | ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

} // namespace

// ---------------- key schedule ----------------

void SecureSession::derive_prk() {
    // PRK = HKDF-Extract(salt = client_random || server_random,
    //                    IKM  = PSK).
    uint8_t salt[2 * SEC_RANDOM_SIZE];
    memcpy(salt, client_random, SEC_RANDOM_SIZE);
    memcpy(salt + SEC_RANDOM_SIZE, server_random, SEC_RANDOM_SIZE);
    hkdf_extract(salt, sizeof(salt), psk, psk_len, prk);
    memset(salt, 0, sizeof(salt));
}

void SecureSession::derive_traffic_key(bool is_tx, uint32_t epoch,
                                       uint8_t* out) {
    // Client->server keys protect the initiator's transmits and the
    // responder's receives; server->client is the mirror. Selecting
    // the label this way makes one peer's tx key equal the other's rx
    // key at the same epoch.
    bool use_c2s = (role == SecRole::Initiator) ? is_tx : !is_tx;
    const uint8_t* lbl = use_c2s ? LBL_C2S : LBL_S2C;
    size_t lbl_len = (use_c2s ? sizeof(LBL_C2S) : sizeof(LBL_S2C)) - 1;

    uint8_t info[sizeof(LBL_S2C) - 1 + 4];
    memcpy(info, lbl, lbl_len);
    store_be32(info + lbl_len, epoch);
    hkdf_expand(prk, info, lbl_len + 4, out, SEC_KEY_SIZE);
}

void SecureSession::finished_mac(bool server,
                                 uint8_t out[SEC_FINISHED_SIZE]) {
    // Bind both nonces and both identities under a key derived from
    // the PRK; a distinct leading byte separates the two directions.
    uint8_t fin_key[SEC_KEY_SIZE];
    hkdf_expand(prk, LBL_FIN, sizeof(LBL_FIN) - 1, fin_key, SEC_KEY_SIZE);

    uint8_t dir = server ? 'S' : 'C';
    HmacSha256 h;
    h.begin(fin_key, SEC_KEY_SIZE);
    h.update(&dir, 1);
    h.update(client_random, SEC_RANDOM_SIZE);
    h.update(server_random, SEC_RANDOM_SIZE);
    // Fold client then server identity in a fixed order regardless of
    // which peer computes the MAC.
    const uint8_t* cid;
    size_t cid_len;
    const uint8_t* sid;
    size_t sid_len;
    if (role == SecRole::Initiator) {
        cid = my_id;         cid_len = my_id_len;
        sid = peer_id_buf;   sid_len = peer_id_length;
    } else {
        cid = peer_id_buf;   cid_len = peer_id_length;
        sid = my_id;         sid_len = my_id_len;
    }
    uint8_t cid_len_byte = (uint8_t)cid_len;
    uint8_t sid_len_byte = (uint8_t)sid_len;
    h.update(&cid_len_byte, 1);
    h.update(cid, cid_len);
    h.update(&sid_len_byte, 1);
    h.update(sid, sid_len);

    uint8_t digest[SHA256_DIGEST_SIZE];
    h.finish(digest);
    memcpy(out, digest, SEC_FINISHED_SIZE);
    memset(digest, 0, sizeof(digest));
    memset(fin_key, 0, sizeof(fin_key));
}

// ---------------- handshake ----------------

void SecureSession::init(SecRole role_, const uint8_t* psk_,
                         size_t psk_len_, const uint8_t* board_id,
                         size_t id_len, const uint8_t* my_random,
                         uint32_t rekey) {
    memset(this, 0, sizeof(*this));
    role = role_;
    state = SecState::Idle;
    psk = psk_;
    psk_len = psk_len_;
    if (id_len > SEC_ID_MAX)
        id_len = SEC_ID_MAX;
    my_id_len = id_len;
    if (id_len)
        memcpy(my_id, board_id, id_len);
    rekey_threshold = rekey ? rekey : SEC_DEFAULT_REKEY;

    // This peer's nonce goes into its role's random slot.
    if (role == SecRole::Initiator)
        memcpy(client_random, my_random, SEC_RANDOM_SIZE);
    else
        memcpy(server_random, my_random, SEC_RANDOM_SIZE);
}

// Serialize a hello (ClientHello or ServerHello share a layout up to
// the identity; ServerHello appends the finished MAC).
static size_t
write_hello(uint8_t* out, uint8_t type, const uint8_t* random,
            const uint8_t* id, size_t id_len) {
    size_t p = 0;
    out[p++] = type;
    out[p++] = SEC_PROTO_VERSION;
    out[p++] = (uint8_t)id_len;
    memcpy(out + p, random, SEC_RANDOM_SIZE);
    p += SEC_RANDOM_SIZE;
    memcpy(out + p, id, id_len);
    p += id_len;
    return p;
}

size_t SecureSession::start(uint8_t* out, size_t cap) {
    if (role != SecRole::Initiator || state != SecState::Idle)
        return 0;
    if (cap < 3 + SEC_RANDOM_SIZE + my_id_len)
        return 0;
    size_t n = write_hello(out, SEC_MSG_CLIENT_HELLO, client_random,
                           my_id, my_id_len);
    state = SecState::WaitServerHello;
    return n;
}

// Parse a hello body (shared prefix). Returns the offset past the
// identity, or 0 on malformed. Fills the given random/id fields.
static size_t
parse_hello(const uint8_t* msg, size_t len, uint8_t want_type,
            uint8_t* random_out, uint8_t* id_out, size_t* id_len_out) {
    if (len < 3 + SEC_RANDOM_SIZE)
        return 0;
    if (msg[0] != want_type || msg[1] != SEC_PROTO_VERSION)
        return 0;
    size_t id_len = msg[2];
    if (id_len > SEC_ID_MAX || len < 3 + SEC_RANDOM_SIZE + id_len)
        return 0;
    memcpy(random_out, msg + 3, SEC_RANDOM_SIZE);
    memcpy(id_out, msg + 3 + SEC_RANDOM_SIZE, id_len);
    *id_len_out = id_len;
    return 3 + SEC_RANDOM_SIZE + id_len;
}

size_t SecureSession::on_handshake(const uint8_t* msg, size_t len,
                                   uint8_t* out, size_t cap) {
    if (len < 1)
        return 0;

    // Responder: ClientHello -> ServerHello.
    if (role == SecRole::Responder && state == SecState::Idle
            && msg[0] == SEC_MSG_CLIENT_HELLO) {
        size_t off = parse_hello(msg, len, SEC_MSG_CLIENT_HELLO,
                                 client_random, peer_id_buf,
                                 &peer_id_length);
        if (!off) {
            state = SecState::Failed;
            return 0;
        }
        derive_prk();
        derive_traffic_key(true, 0, tx_key);
        derive_traffic_key(false, 0, rx_key);
        tx_epoch = rx_epoch = 0;
        tx_seq = 0;
        rx_window_top = 0;
        rx_window_bits = 0;

        if (cap < 3 + SEC_RANDOM_SIZE + my_id_len + SEC_FINISHED_SIZE)
            return 0;
        size_t n = write_hello(out, SEC_MSG_SERVER_HELLO, server_random,
                               my_id, my_id_len);
        uint8_t mac[SEC_FINISHED_SIZE];
        finished_mac(true, mac);
        memcpy(out + n, mac, SEC_FINISHED_SIZE);
        n += SEC_FINISHED_SIZE;
        state = SecState::WaitClientFin;
        return n;
    }

    // Initiator: ServerHello -> ClientFinished.
    if (role == SecRole::Initiator && state == SecState::WaitServerHello
            && msg[0] == SEC_MSG_SERVER_HELLO) {
        size_t off = parse_hello(msg, len, SEC_MSG_SERVER_HELLO,
                                 server_random, peer_id_buf,
                                 &peer_id_length);
        if (!off || len < off + SEC_FINISHED_SIZE) {
            state = SecState::Failed;
            return 0;
        }
        derive_prk();
        uint8_t want[SEC_FINISHED_SIZE];
        finished_mac(true, want);
        // Constant-time compare of the server's proof.
        volatile uint8_t diff = 0;
        for (size_t i = 0; i < SEC_FINISHED_SIZE; i++)
            diff |= (uint8_t)(want[i] ^ msg[off + i]);
        if (diff != 0) {
            auth_failures++;
            state = SecState::Failed;
            return 0;
        }
        derive_traffic_key(true, 0, tx_key);
        derive_traffic_key(false, 0, rx_key);
        tx_epoch = rx_epoch = 0;
        tx_seq = 0;
        rx_window_top = 0;
        rx_window_bits = 0;

        if (cap < 1 + SEC_FINISHED_SIZE)
            return 0;
        out[0] = SEC_MSG_CLIENT_FIN;
        finished_mac(false, out + 1);
        state = SecState::Established;
        return 1 + SEC_FINISHED_SIZE;
    }

    // Responder: ClientFinished completes the handshake.
    if (role == SecRole::Responder && state == SecState::WaitClientFin
            && msg[0] == SEC_MSG_CLIENT_FIN) {
        if (len < 1 + SEC_FINISHED_SIZE) {
            state = SecState::Failed;
            return 0;
        }
        uint8_t want[SEC_FINISHED_SIZE];
        finished_mac(false, want);
        volatile uint8_t diff = 0;
        for (size_t i = 0; i < SEC_FINISHED_SIZE; i++)
            diff |= (uint8_t)(want[i] ^ msg[1 + i]);
        if (diff != 0) {
            auth_failures++;
            state = SecState::Failed;
            return 0;
        }
        state = SecState::Established;
        return 0;
    }

    return 0;
}

// ---------------- data path ----------------

void SecureSession::rekey() {
    tx_epoch++;
    tx_seq = 0;
    derive_traffic_key(true, tx_epoch, tx_key);
}

size_t SecureSession::datagram_encode(uint8_t* out, size_t cap,
                                      const uint8_t* frames, size_t len,
                                      TrafficClass cls) {
    if (state != SecState::Established)
        return 0;
    if (len + SEC_DG_HEADER + SEC_DG_TAG > cap)
        return 0;
    if (len + SEC_DG_HEADER + SEC_DG_TAG > DATAGRAM_MAX)
        return 0;

    out[0] = (uint8_t)(DGF_AUTH | DGF_SESSION
                       | ((uint8_t)cls & DGF_CLASS_MASK));
    out[1] = (uint8_t)tx_epoch;             // low byte identifies epoch
    store_be32(out + 2, tx_seq);
    memcpy(out + SEC_DG_HEADER, frames, len);
    size_t body = SEC_DG_HEADER + len;
    hmac_sha256_tag(tx_key, SEC_KEY_SIZE, out, body, out + body);

    tx_seq++;
    if (tx_seq >= rekey_threshold)
        rekey();
    return body + SEC_DG_TAG;
}

int SecureSession::datagram_decode(uint8_t* data, size_t len,
                                   const uint8_t** frames,
                                   TrafficClass* cls) {
    if (state != SecState::Established)
        return -2;
    if (len < SEC_DG_HEADER + SEC_DG_TAG)
        return -2;
    uint8_t flags = data[0];
    if (!(flags & DGF_SESSION) || !(flags & DGF_AUTH))
        return -2;

    // Epoch is carried as a low byte; reconstruct the full counter
    // relative to the current rx epoch so it survives the 8-bit wrap.
    uint8_t ep_byte = data[1];
    uint32_t base = rx_epoch & ~0xFFu;
    uint32_t epoch = base | ep_byte;
    if ((int32_t)(epoch - rx_epoch) < -128)
        epoch += 256;
    else if ((int32_t)(epoch - rx_epoch) > 128)
        epoch -= 256;
    int32_t ep_delta = (int32_t)(epoch - rx_epoch);

    if (ep_delta < 0) {
        // Authentic-or-not, an older epoch is stale: its window is
        // gone and replaying it must not be accepted.
        old_epoch_rejected++;
        return -3;
    }

    // Verify under the key for the datagram's stated epoch before
    // trusting anything in the header — a forged high epoch must not
    // let an attacker reset our replay window.
    size_t body = len - SEC_DG_TAG;
    uint8_t key[SEC_KEY_SIZE];
    if (ep_delta == 0) {
        memcpy(key, rx_key, SEC_KEY_SIZE);
    } else {
        derive_traffic_key(false, epoch, key);
    }
    uint8_t tag[SEC_DG_TAG];
    hmac_sha256_tag(key, SEC_KEY_SIZE, data, body, tag);
    if (!hmac_tag_equal(tag, data + body)) {
        auth_failures++;
        memset(key, 0, sizeof(key));
        return -1;
    }

    uint32_t seq = load_be32(data + 2);

    if (ep_delta > 0) {
        // Authenticated epoch bump: adopt the new key and restart the
        // replay window on this datagram.
        rx_epoch = epoch;
        memcpy(rx_key, key, SEC_KEY_SIZE);
        rx_window_top = seq;
        rx_window_bits = 1;
    } else {
        // Same epoch: slide the anti-replay window (bit d set means
        // seq (top - d) already seen; bit 0 is the current top).
        if (seq > rx_window_top) {
            uint32_t shift = seq - rx_window_top;
            if (shift >= 64)
                rx_window_bits = 0;
            else
                rx_window_bits <<= shift;
            rx_window_bits |= 1;
            rx_window_top = seq;
        } else {
            uint32_t d = rx_window_top - seq;
            if (d >= 64) {
                replays_rejected++;
                memset(key, 0, sizeof(key));
                return -3;
            }
            if (rx_window_bits & ((uint64_t)1 << d)) {
                replays_rejected++;
                memset(key, 0, sizeof(key));
                return -3;
            }
            rx_window_bits |= ((uint64_t)1 << d);
        }
    }
    memset(key, 0, sizeof(key));

    TrafficClass c = (TrafficClass)(flags & DGF_CLASS_MASK);
    *frames = data + SEC_DG_HEADER;
    *cls = c;
    return (int)(body - SEC_DG_HEADER);
}

} // namespace intentproto
