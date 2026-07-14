# Helix Troubleshooting

> **This is Helix** — an evolution of Klipper. This page covers failure modes
> specific to Helix's new subsystems. New to Helix? Start with the
> **[Helix overview](HELIX.md)**.

This page is *only* the Helix-specific additions. For general 3D-printer and
Klipper troubleshooting — homing faults, thermal errors, `shutdown()` causes,
CAN bus wiring — the inherited docs still apply unchanged: start with the
[FAQ](FAQ.md) and, for CAN links, [CANBUS Troubleshooting](CANBUS_Troubleshooting.md).
The entries below are the ones a stock Klipper page does not cover, because
they arise from subsystems Helix adds: pause-and-hold recovery, machine time,
the authenticated transport, hardware triggers, and per-board capabilities.

Throughout, the host writes to `klippy.log` — the same log file Klipper has
always used. Helix's execution logs (the per-board "flight recorder") are
drained *into* that same file.

A note on maturity: Helix is at 0.9. The recovery, time, and transport
subsystems below are implemented and workstation-tested, but several paths are
validated off-hardware and are still awaiting bring-up on a devkit. Treat the
network and security features as opt-in until you have validated them on your
own bench; the classic Klipper paths are all still present and untouched.

## Recovery and pause-and-hold

### A print PAUSED instead of shutting down

**Symptom.** A recoverable fault occurred, but instead of a full `shutdown()`
the machine paused: motors are still energized and holding position, a heater
with a hold policy is still hot, and a console message describes a paused link
or pause-and-hold rather than a shutdown.

**Likely cause.** This is Helix's flagship behavior working as designed. A
secondary micro-controller configured with `on_comm_timeout: pause` lost its
link (loose cable, AP dropout), or the host stalled, and Helix chose
**pause-and-hold** over abort-everything. The board finished or gently ramped
out its current motion, held position, kept heaters on their per-heater
`failure_policy`, and kept logging. Pause is a *state*, not a death.

**What to check / do.**

1. `FAILURE_RECOVERY_STATUS` — shows the configured heater holds (policy,
   ceiling, duration, and whether each is engaged) and any micro-controllers
   currently in the paused-link state.
2. Fix the underlying cause (reseat the cable, restore the network, clear the
   host stall).
3. `RECONNECT_MCU MCU=<name>` — re-handshake the board that entered
   pause-and-hold.
4. `RESUME_MOTION` — reconcile every joint from its execution log and resume
   the print.

The *why* — what each board preserves, and how resume is reconstructed rather
than guessed — is in
[FD-0001 doc 08](founding/0001-motion-intentions/08-Failure_Recovery.md).

### Managing a heater hold by hand

**Symptom.** A held bed is still hot after a pause and you want to take it back
under host control, or you want to engage a hold deliberately.

**What to do.** `RELEASE_HEATER_HOLD [HEATER=<name>]` returns the heater(s) to
normal host control; `ENGAGE_HEATER_HOLD [HEATER=<name>]` manually engages the
autonomous failsafe hold. `FAILURE_RECOVERY_STATUS` shows which holds are
configured and engaged. Note the honest safety envelope: a held heater keeps
its target autonomously only up to `hold_max_temp` and `hold_max_duration`,
then switches off — see the heater-policy section of
[doc 08](founding/0001-motion-intentions/08-Failure_Recovery.md).

### RESUME_MOTION fails, or one axis won't resume

**Symptom.** `RESUME_MOTION` runs but blocks or refuses for one particular
joint or axis, while the rest of the machine is ready to continue.

**Likely cause.** `RESUME_MOTION` reconciles each joint from its execution log
and resumes; it *blocks only for a joint whose homing was genuinely lost*. That
happens when a joint is marked `motion_homing_volatile: True` **and** its board
actually rebooted (a fresh boot — new uptime, changed config CRC — as opposed
to a link drop with no reset). Helix will not fake a position for a joint whose
homing reference it can no longer trust across that reset.

**What to check / do.** Re-home that axis, then run `RESUME_MOTION` again. If
you did *not* expect a reset, confirm whether the board rebooted at all — a
board that merely lost its link (uptime continuous, config CRC unchanged) keeps
its state authoritative and resumes without a re-home. Only joints you have
explicitly declared volatile require this step; the per-joint recovery model
(retained vs. lost homing, no encoders) is in
[doc 08](founding/0001-motion-intentions/08-Failure_Recovery.md).

## Motion and the segment queue

### Queue UNDERRUN — "the queue ran dry" and the board ramped to a stop

**Symptom.** A trajectory joint decelerated to a controlled stop on its own;
the execution log (execlog = execution log) contains an `underrun` record.

**Likely cause.** The board's segment queue emptied while the joint was still
moving (velocity ≠ 0), so the board took its `motion_underrun_decel`
deceleration ramp to a safe stop rather than executing stale or missing motion.
The queue is a shock absorber for link jitter, but it can be starved by link
loss or congestion, a host stall that stops refilling segments, or a secondary
whose clock went stale (beacon loss beyond the freewheel budget — see
[machine-time](#machine-time--timesync-not-converging) below). This is a
resumable event, not a shutdown.

**What to check / do.**

* `EXECLOG_DUMP` drains the retained micro-controller execution logs — the
  flight recorder — into `klippy.log`, where the `underrun` record and the
  ~100 ms of motion before it become answerable.
* If the underrun came from the link, check the once-a-second `Stats` line in
  `klippy.log` and, on a CAN bus, the `bytes_invalid` counter and wiring per
  [CANBUS Troubleshooting](CANBUS_Troubleshooting.md).
* If it's a secondary, run `TIMESYNC_STATUS` to rule out a stale clock.
* Resume with `RESUME_MOTION` (it rebases the joint and continues). If your
  link is genuinely marginal, `motion_underrun_decel` tunes how hard the board
  brakes when it does run dry.

The underrun ramp and why it beats a mid-print shutdown are in
[doc 08](founding/0001-motion-intentions/08-Failure_Recovery.md) and
[doc 07](founding/0001-motion-intentions/07-Link_Transport.md).

## Networking and time

### A secondary MCU won't reconnect after a link drop

**Symptom.** You've fixed the physical link, but `RECONNECT_MCU` does not
complete and the board stays in the paused-link state.

**Likely cause.** `RECONNECT_MCU MCU=<name>` re-handshakes a board that entered
pause-and-hold. If the board never lost power (uptime continuous, config CRC
unchanged) its entire state is still authoritative and the handshake simply
drains its logs, rebases each joint, and re-disciplines its clock. If the board
actually rebooted, its volatile state is gone and recovery follows the
board-reset path instead (see
[RESUME_MOTION won't resume an axis](#resume_motion-fails-or-one-axis-wont-resume)).

**What to check / do.** Confirm the link is truly restored (cable seated,
network reachable), check `FAILURE_RECOVERY_STATUS` to see the board's paused
state, and retry `RECONNECT_MCU MCU=<name>`. The replugged-toolhead scenario —
what the handshake checks and what it reconstructs — is walked through in
[doc 08](founding/0001-motion-intentions/08-Failure_Recovery.md).

### Machine-time / timesync not converging

**Symptom.** `TIMESYNC_STATUS` reports a secondary as not converged, shows a
large sync error (in microseconds), or a secondary is refusing motion.

**Likely cause.** Each secondary disciplines its local clock to **machine
time** (the primary MCU's counter) using ~1 Hz sync beacons relayed by the
host. Class-0 motion traffic to a board is only enabled *after* its discipline
filter reports convergence. If beacons stop arriving for longer than the
freewheel budget (proposed 5 s), the secondary assumes its clock is stale,
refuses further motion ingest, and — if a joint is mid-motion — takes its
underrun ramp. So a link that can't deliver beacons reliably shows up here as a
board that won't converge or won't accept motion.

**What to check / do.** `TIMESYNC_STATUS` reports, per secondary, whether it is
converged, the current sync error (µs), and the clock-rate correction (ppm =
parts per million). A board stuck far from convergence usually points at the
link carrying its beacons — check link health as for an underrun. The beacon
protocol, the convergence gate, and the freewheel budget are specified in
[FD-0001 doc 01](founding/0001-motion-intentions/01-Time_Model.md).

### Authenticated transport rejects a board

**Symptom.** A networked board (UDP over WiFi/Ethernet) or a v2 serial board
fails to connect, or frames are rejected as unauthenticated.

**Likely cause.** Every datagram on a network transport carries a truncated
**HMAC** (hash-based message authentication code) computed over its contents
plus a nonce/sequence, keyed by a **PSK** (pre-shared key) established at
pairing. This static-PSK HMAC floor is mandatory and the default. A **PSK
mismatch** between host and board, or a failed optional secure-session
handshake (rotating per-session keys and per-board identity), makes the
receiver reject the traffic as forged. Note this is separate from **FEC**
(forward error correction, the negotiable BCH trailer that repairs bit errors)
— an FEC/framing problem shows up as retransmits and CRC naks, not auth
rejects.

**What to check / do.** Verify the PSK matches on both ends and that any
secure-session settings agree. Full key-provisioning and secure-session setup
live on the security page — see **[Secure Networking](Secure_Networking.md)**
(in progress). An unauthenticated mode exists only as an explicit
`trust_network: true` confession for isolated benches. The transport security
model — the mandatory HMAC floor, the optional session layer, and why it's
auth-only — is in
[FD-0001 doc 07](founding/0001-motion-intentions/07-Link_Transport.md).

## Capabilities

### HELIX_STATUS shows a feature you expected as missing

**Symptom.** `HELIX_STATUS` reports a capability you were counting on —
trajectory motion, cubic/quintic segments, hardware triggers, heater hold,
framing v2, or the syscall API — as absent on a particular board.

**Likely cause.** Helix capabilities are **per-board build flags**, not runtime
switches. Each is a firmware Kconfig option (for example `WANT_TRAJECTORY`,
`WANT_TRAJECTORY_HIGHER_ORDER`, `WANT_TRIGGER_SOURCE`, `WANT_HEATER_HOLD`,
`WANT_CONSOLE_FRAMING_V2`, `WANT_SYSCALL_API`) compiled into the
micro-controller image and advertised in its data dictionary. Most default on
where code size allows and off on `HAVE_LIMITED_CODE_SIZE` boards. If the flag
wasn't built in, `HELIX_STATUS` faithfully reports the feature as missing —
because on that board it genuinely is.

**What to check / do.** `HELIX_STATUS` reads each MCU's served capability
dictionary, so it is the ground truth for "what did this board's firmware ship
with." To gain a missing capability, rebuild and reflash that board's firmware
with the matching Kconfig flag enabled. The full flag-to-capability table is in
the [Helix command &amp; feature reference](Helix_Commands.md#firmware-capabilities-kconfig).

### "Quarantine" means two different things

If you encounter the word *quarantine* in Helix docs or messages, disambiguate
by context — it names two unrelated mechanisms:

* **Feature-promotion staging.** New Helix features enter opt-in and
  experimental, prove themselves on real machines, and only then earn promotion
  into the mainline. This is a project/process meaning — see the
  [Helix overview](HELIX.md) and [CONTRIBUTING](CONTRIBUTING.md).
* **ESP32 radio-core isolation.** On the network-native ESP32 target, the WiFi
  radio stack is pinned to one core and isolated behind a byte pipe it cannot
  reach across, so it can't perturb tick-precise motion on the other core. This
  is a hardware/architecture meaning — see [ESP32](ESP32.md) and
  [FD-0001 doc 12](founding/0001-motion-intentions/12-ESP32_Architecture.md).
