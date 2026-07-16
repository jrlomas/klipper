# CANBUS protocol

> **This is Helix** — an evolution of Klipper. This page documents a
> Classical assignment and data traffic remain compatible with upstream
> Klipper. HELIX additionally negotiates ISO CAN-FD carriers and transfers
> machine time with FDCAN hardware timestamps. New to Helix? Start with
> the **[Helix overview](HELIX.md)**.

This document describes the protocol Helix uses to communicate over a
Controller Area Network (CAN) bus (see
[CAN bus](https://en.wikipedia.org/wiki/CAN_bus)). See
[CANBUS.md](CANBUS.md) for information on configuring Helix with CAN
bus.

## Micro-controller id assignment

HELIX always uses 11-bit CAN identifiers. Bootstrap, administration, profile,
time-sync, and recovery frames remain Classical CAN 2.0A with at most eight
data bytes. After a bus-wide transaction succeeds, command byte streams may
use ISO CAN-FD frames containing up to 64 bytes. In order to
support efficient communication, each micro-controller is assigned at
run-time a unique 1-byte CAN bus nodeid (`canbus_nodeid`) for general
Helix command and response traffic. Helix command messages going
from host to micro-controller use the CAN bus id of `canbus_nodeid *
2 + 256`, while Helix response messages from micro-controller to
host use `canbus_nodeid * 2 + 256 + 1`.

Each micro-controller has a factory-assigned full chip identifier. HELIX uses
the typed full value (for example `stm32:<24 hex digits>`) as the canonical
`board_id`. The compatible assignment packet still carries the historical
six-byte `canbus_uuid` hash. A fragmented Classical query resolves that handle
back to the full identifier, includes a CRC, and rejects handle collisions.

## Admin messages

Admin messages are used for id assignment. Admin messages sent from
host to micro-controller use the CAN bus id `0x3f0` and messages sent
from micro-controller to host use the CAN bus id `0x3f1`. All
micro-controllers listen to messages on id `0x3f0`; that id can be
thought of as a "broadcast address".

### CMD_QUERY_UNASSIGNED message

This command queries all micro-controllers that have not yet been
assigned a `canbus_nodeid`. Unassigned micro-controllers will respond
with a RESP_NEED_NODEID response message.

The CMD_QUERY_UNASSIGNED message format is:
`<1-byte message_id = 0x00>`

### CMD_SET_KLIPPER_NODEID message

This command assigns a `canbus_nodeid` to the micro-controller with a
given `canbus_uuid`.

The CMD_SET_KLIPPER_NODEID message format is:
`<1-byte message_id = 0x01><6-byte canbus_uuid><1-byte canbus_nodeid>`

### RESP_NEED_NODEID message

The RESP_NEED_NODEID message format is:
`<1-byte message_id = 0x20><6-byte canbus_uuid><1-byte set_klipper_nodeid = 0x01>`

### CMD_QUERY_BOARD_ID and RESP_BOARD_ID

The host sends
`<0x03><6-byte canbus_uuid><1-byte offset>`. The matching node replies on
`0x3f1` with `<0x21><family><total_length><offset><3 data bytes><crc8>`.
Offsets advance by three until the full factory identifier is reconstructed.
The CRC covers family, length, and the complete raw identifier. More than one
distinct reply for the same legacy handle is a fatal identity collision.

## Data Packets

A micro-controller that has been assigned a nodeid via the
CMD_SET_KLIPPER_NODEID command can send and receive data packets.

The packet data in messages using the node's receive CAN bus id
(`canbus_nodeid * 2 + 256`) are simply appended to a buffer, and when
a complete [mcu protocol message](Protocol.md) is found its contents
are parsed and processed. The data is treated as a byte stream - there
is no requirement for the start of a Klipper message block to align
with the start of a CAN bus packet.

Similarly, mcu protocol message responses are sent from
micro-controller to host by copying the message data into one or more
packets with the node's transmit CAN bus id (`canbus_nodeid * 2 +
256 + 1`).

Before profile activation those packets carry at most eight bytes. An active
HELIX FD profile permits DLC lengths 12, 16, 20, 24, 32, 48, and 64; the
framed MCU protocol remains a byte stream, so its message boundaries need not
align with CAN frames. FDF/BRS/ESI are represented explicitly and remote
request frames are never encoded as FD.

## Profile transaction

Every node reports its 64-byte support, exact 1/2/5/8 Mbit data-rate mask, and
declared transceiver ceiling. The host selects only a unanimous profile and
performs `prepare -> commit -> Linux netdevice apply/readback -> enable` under
a fresh epoch. Failure invokes `abort` on prepared nodes and explicitly
returns the netdevice to Classical 1 Mbit. Classical-only participants cause a
refusal or a separately configured fallback transaction; they can not coexist
actively with FD frames on one electrical segment.

## Hardware-timestamped machine time

`0x080` is `CAN_TIME_SYNC` and `0x081` is `CAN_TIME_FOLLOW_UP`; both are short
Classical frames. The sync contains magic `0x48`, type `0x01`, sequence,
quality, and a 32-bit epoch. It requests an FDCAN Tx Event. Each node retains
the RX element's hardware start-of-frame clock. The follow-up contains magic
`0x48`, type `0x02`, the same sequence/quality, and the primary machine clock
translated from the bridge's exact Tx Event timestamp. Thus arbitration,
ISR-service, and follow-up delivery delays are excluded from the discipline
sample.

## Retransmission

Controller automatic retransmission remains enabled for arbitration loss and
isolated line errors. STM32 FDCAN transmit buffers are nevertheless bounded:
normal protocol traffic is cancelled after 25 ms and administration/time
traffic after 100 ms. The framed protocol's ACK and sequence layer remains the
end-to-end delivery authority. A burst of eight FD protocol errors in 10 ms or
bus-off suppresses FD emission, initiates Classical recovery, and latches a
transport shutdown instead of continuing stale motion traffic.
