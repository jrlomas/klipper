#ifndef INTENTPROTO_CORE_IDS_HPP
#define INTENTPROTO_CORE_IDS_HPP
// Fixed message ids for the v2 core command set (FD-0001 docs 02,
// 08, 09, 10, 11).
//
// In the v2 protocol the dictionary is demoted: the core command set
// every board must answer gets stable ids frozen here in the spec —
// VLQ encoding makes dense per-build numbering worthless — so a v2
// peer needs no dictionary round-trip at all. This header IS that
// allocation: documentation-as-header, plain constants, nothing to
// link. The legacy protocol keeps its per-build ids assigned by
// init() and its JSON dictionary; the two numbering schemes never
// mix on one link.
//
// Allocation rules:
//   * 0 and 1 are fixed by the legacy protocol and retained.
//   * 2..0x7f is the spec-frozen core space, assigned below in
//     blocks; unused values are reserved for future core commands
//     and MUST NOT be reused by applications.
//   * ids >= 0x80 (MSGID_EXTENSION_BASE) are the extension space:
//     device-specific commands self-describe over a fixed
//     meta-command and the host binds to them at connect. Nothing
//     in the extension space is ever spec-frozen.

#include <stdint.h>

namespace intentproto {
namespace v2 {

// ---- fixed by the legacy protocol (retained) ----
constexpr uint32_t MSGID_IDENTIFY_RESPONSE = 0;
constexpr uint32_t MSGID_IDENTIFY = 1;

// ---- clock / uptime / config / stats basics ----
// get_clock -> clock: the low 32 bits of the MCU clock (the clock
// discipline loop's sample; see doc 01).
constexpr uint32_t MSGID_GET_CLOCK = 2;
constexpr uint32_t MSGID_CLOCK = 3;
// get_uptime -> uptime: 64-bit tick count; a reconnect handshake
// showing a fresh boot means volatile state is gone (doc 08).
constexpr uint32_t MSGID_GET_UPTIME = 4;
constexpr uint32_t MSGID_UPTIME = 5;
// get_config -> config: is_config flag, config CRC, move pool size.
constexpr uint32_t MSGID_GET_CONFIG = 6;
constexpr uint32_t MSGID_CONFIG = 7;
// get_stats -> stats: load counters, per-class link accounting
// (doc 03).
constexpr uint32_t MSGID_GET_STATS = 8;
constexpr uint32_t MSGID_STATS = 9;

// ---- trajectory intentions (doc 02) ----
constexpr uint32_t MSGID_CONFIG_TRAJECTORY = 10;
constexpr uint32_t MSGID_TRAJECTORY_REBASE = 11;
constexpr uint32_t MSGID_QUEUE_TRAJ_SEGMENT = 12;
constexpr uint32_t MSGID_TRAJ_HOLD = 13;
constexpr uint32_t MSGID_TRAJ_GET_POSITION = 14;
// MCU -> host:
constexpr uint32_t MSGID_TRAJ_POSITION = 15;   // reply, Class 1
constexpr uint32_t MSGID_TRAJ_UNDERRUN = 16;   // event, Class 1
constexpr uint32_t MSGID_TRAJ_STATUS = 17;     // telemetry, Class 2

// ---- execution log (doc 08) ----
// query -> status reports the log's high-water marks; dump -> data
// is the Class-1 reliable pull the resume workflow drains.
constexpr uint32_t MSGID_EXECLOG_QUERY = 18;
constexpr uint32_t MSGID_EXECLOG_DUMP = 19;
constexpr uint32_t MSGID_EXECLOG_STATUS = 20;
constexpr uint32_t MSGID_EXECLOG_DATA = 21;

// ---- heater failure-policy hold (doc 08) ----
// config sets policy/ceilings; state reports hold entry/exit and the
// autonomous-controller status.
constexpr uint32_t MSGID_HEATER_HOLD_CONFIG = 22;
constexpr uint32_t MSGID_HEATER_HOLD_STATE = 23;

// ---- hardware trigger sources (doc 09) ----
constexpr uint32_t MSGID_CONFIG_TRIGGER_SOURCE = 24;
constexpr uint32_t MSGID_TRIGGER_SOURCE_ARM = 25;
constexpr uint32_t MSGID_TRIGGER_EVENT = 26;   // event, Class 1

// ---- bootloader / in-band update (doc 11) ----
constexpr uint32_t MSGID_ENTER_BOOTLOADER = 27;
constexpr uint32_t MSGID_FLASH_BEGIN = 28;
constexpr uint32_t MSGID_FLASH_DATA = 29;
constexpr uint32_t MSGID_FLASH_VERIFY = 30;
constexpr uint32_t MSGID_FLASH_BOOT = 31;

// ---- extension self-description (doc 10) ----
// The fixed meta-commands a v2 peer enumerates the extension space
// with instead of a dictionary round-trip; wire format documented in
// proto.hpp ("extension self-description"). On a legacy link the
// same messages exist but carry init()-assigned ids like any other
// registered command (the two numbering schemes never mix).
constexpr uint32_t MSGID_LIST_EXTENSIONS = 32;
constexpr uint32_t MSGID_EXTENSION_DESC = 33;
constexpr uint32_t MSGID_LIST_CONSTANTS = 34;
constexpr uint32_t MSGID_CONSTANT_DESC = 35;
constexpr uint32_t MSGID_EXTENSION_DONE = 36;

// First id past the frozen allocation; 37..0x7f reserved for future
// core commands.
constexpr uint32_t MSGID_CORE_NEXT_FREE = 37;

// Extension space: device-specific, self-described at connect.
constexpr uint32_t MSGID_EXTENSION_BASE = 0x80;

} // namespace v2
} // namespace intentproto

#endif // INTENTPROTO_CORE_IDS_HPP
