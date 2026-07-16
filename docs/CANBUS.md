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

Before a bridge firmware restart, Klippy quiesces every downstream node to the
permanent Classical 1 Mbit recovery floor, stops the time beacon, and only then
resets the USB bridge.

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
