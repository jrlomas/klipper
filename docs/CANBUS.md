# CANBUS

> **This is Helix** — an evolution of Klipper. This page documents
> Controller Area Network (CAN) bus support in Helix. Classical CAN remains
> upstream-compatible; Helix also provides a transactional ISO CAN-FD mode.
> New to Helix? Start with the
> **[Helix overview](HELIX.md)**.

This document describes Helix's Controller Area Network (CAN) bus support.

## Device Hardware

Helix currently supports CAN on stm32, SAME5x, and rp2040 chips. In
addition, the micro-controller chip must be on a board that has a CAN
transceiver.

To compile for CAN, run `make menuconfig` and select "CAN bus" as the
communication interface. Finally, compile the micro-controller code
and flash it to the target board.

## Host Hardware

In order to use a CAN bus, it is necessary to have a host adapter. It
is recommended to use a "USB to CAN adapter". There are many different
USB to CAN adapters available from different manufacturers. When
choosing one, we recommend verifying that the firmware can be updated
on it. (Unfortunately, we've found some USB adapters run defective
firmware and are locked down, so verify before purchasing.) Look for
adapters that can run Helix directly (in its "USB to CAN bridge
mode") or that run the
[candlelight firmware](https://github.com/candle-usb/candleLight_fw).

It is also necessary to configure the host operating system to use the
adapter. This is typically done by creating a new file named
`/etc/network/interfaces.d/can0` with the following contents:
```
allow-hotplug can0
iface can0 can static
    bitrate 1000000
    up ip link set $IFACE txqueuelen 128
```

## Terminating Resistors

A CAN bus should have two 120 ohm resistors between the CANH and CANL
wires. Ideally, one resistor located at each the end of the bus.

Note that some devices have a builtin 120 ohm resistor that can not be
easily removed. Some devices do not include a resistor at all. Other
devices have a mechanism to select the resistor (typically by
connecting a "pin jumper"). Be sure to check the schematics of all
devices on the CAN bus to verify that there are two and only two 120
Ohm resistors on the bus.

To test that the resistors are correct, one can remove power to the
printer and use a multi-meter to check the resistance between the CANH
and CANL wires - it should report ~60 ohms on a correctly wired CAN
bus.

## Finding a board identity

HELIX CAN uses the full typed factory identifier as its canonical identity;
the historical six-byte `canbus_uuid` hash is retained only for compatible
Classical assignment frames. With the interface in Classical 1 Mbit bootstrap
mode, run:

```
~/klippy-env/bin/python ~/Projects/klipper/scripts/helix_can_scan.py helixcan0
```

The result includes a value such as
`board_id=stm32:00112233445566778899aabb` and its legacy assignment handle.
If two boards ever produce the same six-byte handle, discovery refuses the
collision instead of guessing. The old query below remains valid for legacy
configurations.

### Legacy canbus_uuid discovery

Each micro-controller on the CAN bus is assigned a unique id based on
the factory chip identifier encoded into each micro-controller. To
find each micro-controller device id, make sure the hardware is
powered and wired correctly, and then run:
```
~/klippy-env/bin/python ~/klipper/scripts/canbus_query.py can0
```

If uninitialized CAN devices are detected the above command will
report lines like the following:
```
Found canbus_uuid=11aa22bb33cc, Application: Klipper
```

Each device will have a unique identifier. In the above example,
`11aa22bb33cc` is the micro-controller's "canbus_uuid".

Note that the `canbus_query.py` tool will only report uninitialized
devices - if Helix (or a similar tool) configures the device then it
will no longer appear in the list.

## Configuring Klipper

Update the Helix [mcu configuration](Config_Reference.md#mcu) to use
the CAN bus to communicate with the device - for example:
```
[mcu my_can_mcu]
canbus_uuid: 11aa22bb33cc
```

The HELIX named-bus form is preferred for CAN-FD:

```
[mcu canbridge]
serial: /dev/helix-can-bridge

[helix_can helixcan0]
interface: helixcan0
bridge_mcu: canbridge
preferred_profiles: FD_1M_NOBRS
classic_node_policy: refuse

[mcu ebb36]
canbus: helixcan0
board_id: stm32:00112233445566778899aabb
```

Every required node attaches in Classical 1 Mbit mode first. HELIX intersects
the controller/transceiver capabilities, stages the same profile on every
node, asks Linux to apply and read back the SocketCAN timing, and enables FD
only after the complete transaction succeeds. A profile request is never
silently rounded or downgraded.

`[helix_can]` now owns a transport-neutral `CanFabricEndpoint`. The normal USB
bridge still projects through AF_CAN and remains the default; an authenticated
Ethernet gateway may project the same fabric without inventing a CAN UUID or
changing any downstream `board_id`. Versioned status keeps profile/owner
epochs, board identities, and the accepted/forwarded/dropped/queued
conservation identity above that backend choice. Atlas treats ordinary status
as telemetry and only records explicit gateway incidents.

The typed Ethernet gateway adds selective packet acknowledgements, but it does
not turn raw CAN into a replayable datagram stream. Idempotent profile/status
controls may retry within a fixed window. If a CAN or serial packet times out,
its delivery becomes `UNKNOWN`; upper Klipper/HELIX recovery decides what to
do, and the proxy never duplicates an actuator frame merely to improve packet
statistics.

## USB to CAN bus bridge mode

### HELIX composite bridge

The HELIX bridge enumerates as manufacturer `OpenAMS`, product
`Helix CAN-FD Bridge`. Interface zero binds to mainline Linux `gs_usb`; two
additional interfaces form a CDC-ACM control console. The bridge is configured
with `serial: /dev/helix-can-bridge` and is not assigned a synthetic CAN node
or fake CAN UUID. The supplied systemd `.link` rule names the network device
`helixcan0`; the privileged manager accepts only allowlisted profile names and
interfaces over `/run/helix/helix-can-manager.sock`.

The conservative current-hardware profile is `FD_1M_NOBRS`: it uses 64-byte
ISO CAN-FD frames at a 1 Mbit/s nominal/data rate and therefore remains inside
the existing transceivers' electrical ceiling. Later 2/5/8 Mbit/s BRS profiles
use the same protocol and state machine but require suitable transceivers.

The FPS composite bridge and EBB36 v1.2 physically qualified this conservative
profile on 2026-07-16. Linux read back MTU 72 and 1 Mbit/s nominal/data timing;
all legal physical payload sizes (0..8, 12, 16, 20, 24, 32, 48, and 64) were
emitted and captured with `can-utils`, and the controller retained zero error,
drop, retry, and bus-off growth. Repeated Klipper process restarts also
completed the session-reset handshake without cycling either board.

Longer passive captures then reproduced an important limitation familiar from
stock Klipper CAN deployments: a frame could be absent from the host capture
while Linux still reported zero dropped or missed packets, and while the FDCAN
FIFO and bridge forwarding queue also reported zero loss. The original FD byte
stream split a 22-byte protocol block into 20- and 2-byte frames, so loss of
the tail corrupted later framing. HELIX now packs one or more complete raw
protocol messages into each CAN-FD frame, never splits a message, and uses each
in-band length to discard final DLC padding. This preserves upstream sequence
batching while removing partial-message loss propagation; bridge counters now distinguish frames
accepted by FDCAN from frames handed to USB.

Physical requalification then exposed the finite configuration-response burst
that SocketCAN's counters had hidden. A packed-carrier 256-entry bridge lost
124 of 4,000 accepted frames at a full queue. The qualified 512-entry bridge
forwarded all 37,288 accepted frames across repeated cold/session reconnects,
drained to depth zero, reached a bounded high-water mark of 434, and reported
zero queue drops or unaccounted handoff. A 1,013-frame capture decoded 1,070
complete protocol records (56 multi-record frames) with no malformed record.
Three subsequent profile transitions reported zero stale-carrier bytes. This
closes the packed-carrier/startup-burst gate, but is not yet CAN homing,
extrusion, print, injected bus-off, or BRS qualification.

A later mixed-motion print kept the bridge conservation invariant exact but
exposed repeated receive loss inside the EBB36: its aggregate receive counter
rose from zero to 11,114, Klippy retransmitted 669,735 bytes, and a 537 ms ACK
stall exhausted the extruder trajectory horizon. The recovery hold paused and
then resumed coherently. STM32 FDCAN firmware now drains every element pending
in its three-slot hardware FIFO per interrupt and separately reports FIFO
overruns, physical protocol errors, and FIFO high-water.

Physical requalification passed on 2026-07-17 with firmware `219569e9` on
both the FPS bridge and EBB36. The same 26-minute PLA cube completed in
1,733.405 seconds of motion and used 3,637.983 mm of filament. The EBB36
received 4,577,247 bytes with zero retransmitted or invalid bytes; its FIFO
high-water reached two of three entries while FIFO overruns, protocol errors,
and the compatibility receive-error aggregate remained zero. Final bridge
accounting reported 236,528 accepted and forwarded frames, depth zero,
high-water three, and zero drops or unaccounted handoff. SocketCAN remained
`ERROR-ACTIVE` with zero errors, drops, missed frames, warning/passive
transitions, or bus-offs. This closes the receive-drain print regression at
the 1 Mbit `FD_1M_NOBRS` profile.

A subsequent long-run audit found a second, rarer node-side boundary: the
three-frame reliable receive window and independent time/admin frames all used
the same three-entry FDCAN FIFO. The ISR could drain correctly and still arrive
too late to recover an overwritten fourth frame. Helix now maps reliable data
to FIFO0 and timing/control to FIFO1, acknowledges copied entries before
protocol dispatch, and exposes per-FIFO loss/high-water plus a conservative
start-of-frame-to-service maximum. A no-motion/no-heater 500-iteration hardware
stress held EBB36 interrupts for 2 ms while filling the command credit; FIFO0
and FIFO1 high-water both reached two with zero loss, retransmission, invalid
bytes, or SocketCAN drops. The conservative service maximum was 152,153 ticks
(2.377 ms, including frame wire time). The diagnostic command
`HELIX_CAN_RX_STRESS` is firmware-capped at the proven-safe 2 ms.

After the first coherent resume, continued receive loss caused a second
extruder underrun and then a delayed software-PWM update triggered `Timer too
close`. Current Helix firmware applies late software-PWM state promptly while
retaining the independent maximum-duration heater watchdog. Stale prompt
output traffic therefore cannot turn an already-established trajectory
recovery hold into a global MCU shutdown; motion commands remain fail-closed.

Before a bridge firmware restart, Klippy quiesces every downstream node to the
permanent Classical 1 Mbit recovery floor, stops the time beacon, and only then
resets the USB bridge.

Use `HELIX_CAN_STATUS BUS=helixcan0` to inspect the active negotiation without
changing it. The report includes the selected profile and rates, transaction
and time epochs, required nodes, cumulative controller errors and retries, and
the bridge's accepted-to-forwarded queue accounting. `delivery=OK` means no
queue drops or unaccounted handoff have been observed; because the counters are
cumulative, compare their deltas across a print when diagnosing an incident.

For an operator-controlled bridge flash, run
`HELIX_CAN_QUIESCE BUS=helixcan0` and wait for its confirmation, then stop
Klipper before triggering the bootloader. The explicit maintenance command
waits for motion to drain, aborts the FD profile on every node, stops the time
source, and changes `helixcan0` to Classical 1 Mbit. Stopping Klipper prevents
its normal reconnect path from immediately negotiating FD again while the
operator prepares the flash.

Retained Katapult/CanBoot images may have a different fixed Classical bitrate.
Use, for example,
`HELIX_CAN_QUIESCE BUS=helixcan0 PROFILE=CLASSIC_500K` for a known 500 kbit
bootloader, then stop Klipper. The allowlisted `CLASSIC_125K`, `CLASSIC_250K`,
and `CLASSIC_500K` profiles are maintenance-only and can never win application
profile negotiation. The composite bridge now accepts exact nominal timing
from SocketCAN; physical readback on the FPS bridge confirmed runtime timing
changes including 500 kbit Classical and 1 Mbit Classical/FD operation. The
manager remains the only `CAP_NET_ADMIN` holder and rejects arbitrary rates.

The EBB36 v1.2's retained vendor Katapult image is a separate known hardware
maintenance defect. Its Helix application accepted the verified reboot handle,
but bootloader `v0.0.1-79-g25a23cd` answered on none of 125/250/500 kbit or
1 Mbit and did not enumerate on USB, while the FPS bridge applied and read back
each rate. Replace it through DFU with a known PB0/PB1, 8 MHz-reference,
1 Mbit Katapult build before relying on CAN application updates. The marker
`CanBoot!` in its flash is Katapult's retained compatibility marker; it does
not mean the installed image has a working transport configuration.

Install the checked-in host integration once (the service unit currently uses
this workstation's `/home/jrlomas/Projects/klipper` checkout):

```
sudo install -m 0644 ~/Projects/klipper/scripts/systemd/73-helix-can.link /etc/systemd/network/73-helix-can.link
sudo install -m 0644 ~/Projects/klipper/scripts/udev/73-helix-can.rules /etc/udev/rules.d/73-helix-can.rules
sudo install -m 0644 ~/Projects/klipper/scripts/systemd/helix-can-manager.service /etc/systemd/system/helix-can-manager.service
sudo systemctl daemon-reload
sudo udevadm control --reload
sudo systemctl enable --now helix-can-manager.service
```

The service runs with only `CAP_NET_ADMIN`, accepts fixed profile names and an
allowlisted interface, and exposes its socket to the workstation's `jrlomas`
service group (change both unit arguments if the Klipper service user differs). Replug
the bridge after installing the naming rules; do not rename a live CAN device
during a print.

### Legacy upstream-compatible bridge

Some micro-controllers support the historical "USB to CAN bus bridge" mode
during Helix's "make menuconfig". This mode may allow one to use a
micro-controller as both a "USB to CAN bus adapter" and as a Helix
node.

When Helix uses this mode the micro-controller appears as a "USB CAN
bus adapter" under Linux. The "Helix bridge mcu" itself will appear
as if it was on this CAN bus - it can be identified via
`canbus_query.py` and it must be configured like other CAN bus Helix
nodes.

Some important notes when using this mode:

* It is necessary to configure the `can0` (or similar) interface in
  Linux in order to communicate with the bus. However, Linux CAN bus
  speed and CAN bus bit-timing options are ignored by Klipper.
  Currently, the CAN bus frequency is specified during "make
  menuconfig" and the bus speed specified in Linux is ignored.

* Whenever the "bridge mcu" is reset, Linux will disable the
  corresponding `can0` interface. To ensure proper handling of
  FIRMWARE_RESTART and RESTART commands, it is recommended to use
  `allow-hotplug` in the `/etc/network/interfaces.d/can0` file. For
  example:
```
allow-hotplug can0
iface can0 can static
    bitrate 1000000
    up ip link set $IFACE txqueuelen 128
```

* The "bridge mcu" is not actually on the CAN bus. Messages to and
  from the bridge mcu will not be seen by other adapters that may be
  on the CAN bus.

* The available bandwidth to both the "bridge mcu" itself and all
  devices on the CAN bus is effectively limited by the CAN bus
  frequency. As a result, it is recommended to use a CAN bus frequency
  of 1000000 when using "USB to CAN bus bridge mode".

* It is only valid to use USB to CAN bridge mode if there is a
  functioning CAN bus with at least one other node available (in
  addition to the bridge node itself). Use a standard USB
  configuration if the goal is to communicate only with the single USB
  device. Using USB to CAN bridge mode without a fully functioning CAN
  bus (including terminating resistors and an additional node) may
  result in sporadic errors even when communicating with the bridge
  node.

* A USB to CAN bridge board will not appear as a USB serial device, it
  will not show up when running `ls /dev/serial/by-id`, and it can not
  be configured in Helix's printer.cfg file with a `serial:`
  parameter. The bridge board appears as a "USB CAN adapter" and it is
  configured in the printer.cfg as a [CAN node](#configuring-klipper).

## Tips for troubleshooting

See the [CAN bus troubleshooting](CANBUS_Troubleshooting.md) document.
