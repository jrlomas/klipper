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

Before profile activation those packets carry at most eight bytes. Under an
active HELIX FD profile, one CAN-FD payload packs as many *complete* framed MCU
protocol messages as fit in 64 bytes. Each raw message retains its own length,
CRC, sync trailer, and sequence. This preserves Kevin O'Connor's host write
batching from commit `c5968a08` instead of imposing one sequence per transport
frame. A raw message is never split between FD frames.

If the packed logical length falls in a DLC gap (9..11, 13..15, and so on),
the sender selects the smallest legal physical DLC and zero-pads only after the
last complete message. The receiver walks each message's in-band length and
ignores that final padding. Thus an isolated 22-byte message uses a 24-byte
physical frame, several short messages with distinct sequences may share a
single 64-byte frame, and a full 64-byte message still fits by itself.
FDF/BRS/ESI are represented explicitly and remote-request frames are never
encoded as FD.

This packing rule is also the loss-containment boundary. Losing a CAN-FD frame
may lose several complete sequenced messages, but it cannot leave a two-byte
tail missing and concatenate the next frame onto an incomplete predecessor.
The existing protocol sequence and retransmission machinery detects
command-stream loss.
Linux SocketCAN drop counters remain insufficient evidence of end-to-end
delivery, so the bridge separately exposes physical receive, forwarding,
FIFO-loss, queue-drop, and queue-high-water counters. The G0B1 composite bridge
uses a physically qualified 512-frame staging queue for USB scheduling
elasticity. Complete-message
packing materially reduces the number of fixed-size `gs_usb` FD records; queue
capacity is not permission to advertise a sustained CAN profile whose encoded
USB rate exceeds USB Full Speed capacity.

### Bridge forwarding-capacity invariant

Every CAN bridge MUST satisfy this inequality for each admitted profile under
the qualified workload:

```text
effective host-link service rate
    > encoded CAN-to-host offered rate
```

The comparison is made after transport encoding, not from link labels such as
"12 Mbit USB" and "8 Mbit CAN-FD". For `gs_usb`, each received CAN-FD frame
occupies a fixed host record and one or more USB transactions even when the CAN
payload is short. USB framing, host scheduling, endpoint cadence, CAN
arbitration/data-phase ratios, and achieved multi-message packing density all
enter the measurement. A queue may absorb a finite measured burst only when it
returns to baseline and accepted/forwarded conservation remains lossless. A
queue that grows with test duration proves the profile inadmissible.

Raw USB Full Speed bitrate alone therefore does not qualify 2/5/8 Mbit BRS.
Each profile requires a sustained saturation test showing zero FIFO and queue
drops, `hw_rx_frames == usb_forwarded_frames + rx_queue_depth`, a bounded
high-water mark with engineering margin, and complete drain after producer
load stops. A faster host transport is required when that gate cannot pass.

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
end-to-end delivery authority. Physical stuff/form/CRC/bit/ACK errors remain
visible in cumulative diagnostics, while the CAN controller's standard error
confinement and retransmission handle transient bursts. A malformed Helix FD
carrier still latches after eight observations in 10 ms because that indicates
a software/profile mismatch rather than line noise. Hardware bus-off is the
physical fail-closed boundary: it suppresses FD emission and latches transport
shutdown instead of continuing stale motion traffic.

Restarting the composite bridge briefly removes and recreates the SocketCAN
netdevice. The privileged profile manager retries only transient
missing-device, broken-pipe, network-down, and early-link-up failures for a
bounded three seconds. This closes the CDC-before-`gs_usb` enumeration race
without masking permanent bit-timing or profile errors.
