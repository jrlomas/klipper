# Multiple Micro-controller Homing and Probing

> **This is Helix** — an evolution of Klipper. This page documents homing
> across multiple micro-controllers in Helix; the capability is shared with
> upstream Klipper. New to Helix? Start with the
> **[Helix overview](HELIX.md)**.

Helix supports a mechanism for homing with an endstop attached to
one micro-controller unit (MCU) while its stepper motors are on a different
micro-controller. This support is referred to as "multi-mcu
homing". This feature is also used when a Z probe is on a different
micro-controller than the Z stepper motors.

This feature can be useful to simplify wiring, as it may be more
convenient to attach an endstop or probe to a closer micro-controller.
However, using this feature may result in "overshoot" of the stepper
motors during homing and probing operations.

The overshoot occurs due to possible message transmission delays
between the micro-controller monitoring the endstop and the
micro-controllers moving the stepper motors. The Helix code is
designed to limit this delay to no more than 25ms. (When multi-mcu
homing is activated, the micro-controllers send periodic status
messages and check that corresponding status messages are received
within 25ms.)

So, for example, if homing at 10mm/s then it is possible for an
overshoot of up to 0.250mm (10mm/s * .025s == 0.250mm). Care should be
taken when configuring multi-mcu homing to account for this type of
overshoot. Using slower homing or probing speeds can reduce the
overshoot.

Stepper motor overshoot should not adversely impact the precision of
the homing and probing procedure. The Helix code will detect the
overshoot and account for it in its calculations. However, it is
important that the hardware design is capable of handling overshoot
without causing damage to the machine.

In order to use this "multi-mcu homing" capability the hardware must
have predictably low latency between the host computer and all of the
micro-controllers. Typically the round-trip time must be consistently
less than 10ms. High latency (even for short periods) is likely to
result in homing failures.

Helix retains the 25ms watchdog by default. A network MCU whose latency
distribution has been measured and bounded may set
`multi_mcu_homing_timeout` in that MCU's configuration section. The largest
configured value among the endstop and motion MCUs is applied to that homing
group. The event is still propagated as soon as it arrives; this option does
not intentionally delay a successful stop. It only allows trsync to survive
a longer liveness gap. The configured timeout must therefore be treated as a
mechanical overshoot bound during a complete link outage:
`homing_speed * multi_mcu_homing_timeout`.

An `[intentproto_transport]` also publishes the minimum liveness window its
carrier requires. Datagram mode defaults this floor to 250ms because its
end-to-end serial ARQ can legitimately reach a 200ms retry backoff after
consecutive packet losses. The effective group timeout is the largest MCU or
carrier value; a shorter value in `[mcu]` cannot silently undercut the
carrier. Set `multi_mcu_homing_timeout` in the transport section only after
qualifying both the link latency distribution and the resulting mechanical
overshoot bound.

Trsync watchdog renewals are control traffic and become eligible for
transmission at the status observation that justified the renewal. They are
not held until the newly proposed expiry. This distinction is immaterial for
the classic 25ms watchdog (shorter than serialqueue's normal 100ms send
horizon), but is required for a longer qualified network watchdog: delaying a
renewal until 100ms before its *new* expiry can place it after the *old*
expiry.

Should high latency result in a failure (or if some other
communication issue is detected) then Helix will raise a
"Communication timeout during homing" error. (This overshoot-and-timeout
behavior stems from the classic step-stream approach; Helix's
hardware-event homing keeps the trigger decision on the micro-controller
itself and can pause-and-hold rather than fail on a transient hiccup — see
the **[Helix overview](HELIX.md)**.)

Note that an axis with multiple steppers (eg, `stepper_z` and
`stepper_z1`) need to be on the same micro-controller in order to use
multi-mcu homing. For example, if an endstop is on a separate
micro-controller from `stepper_z` then `stepper_z1` must be on the
same micro-controller as `stepper_z`.
