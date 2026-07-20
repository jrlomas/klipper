# FD-0001: Autonomous Heater Control

## Decision

Helix divides thermal control into two planes:

* the **host configuration and supervision plane** selects the sensor model,
  target, gains, limits, and loss policy, runs system identification, records
  telemetry, and presents operator controls;
* the **MCU execution plane** acquires the sensor, evaluates the controller,
  enforces local limits, and drives PWM on a fixed cadence.

After configuration, loss of host traffic does not stop a healthy controller.
The MCU keeps the most recently accepted profile and target in RAM until the
configured autonomous-duration limit. This is continuity through a host or
transport outage, not persistence through an MCU reset. Firmware shutdown or
reset always makes the heater output inactive.

Reset persistence is deliberately a later, separate feature. It requires a
versioned profile, CRC, monotonic generation, wear-safe storage, boot-time
sensor validation, and an explicit re-arm policy. A heater must never come on
merely because old flash contains a plausible target.

## Why move the loop

Base Klipper evaluates PID in `klippy/extras/heaters.py` when a temperature
report reaches Python, then schedules another PWM update back to the MCU. The
MCU `max_duration` watchdog correctly turns the output off if those refreshes
stop. That arrangement is safe, but transport latency, host scheduling, Python
delivery, and control arithmetic are one serial dependency chain.

Kalico/Danger-Klipper provide useful control work to learn from:

| Control | Useful property | Helix conclusion |
| --- | --- | --- |
| Positional PID | Robust and familiar | Keep compatibility with existing Klipper gains, but add explicit derivative filtering and anti-windup |
| Velocity PID | Avoids derivative kick and can reduce target overshoot | Derivative-on-measurement provides the essential no-kick property in the first MCU controller; retain velocity form as an ABI extension |
| PID profiles | Runtime-selectable tuned operating points | Profiles belong to the host; activation uploads one bounded MCU profile |
| MPC | Models block, sensor, ambient, fan, and filament heat flow | Valuable later, but fan/extrusion feed-forward inputs must also be locally available or have declared stale behavior before an MCU MPC can be autonomous |
| Dual-loop PID | Separates surface and heater-medium sensors | Reserve multiple local ADC consumers; do not claim dual-loop support until both sensors and cross-checks execute on one MCU |

The goal is not a faster PWM switching frequency. Heater plants are slow. The
gain is deterministic sampling, no round trip in the feedback loop, and a
defined response when the host disappears.

## Data path

```text
ADC trigger -> hardware oversampling -> DMA ping-pong buffer
            -> exact firmware boxcar -> local safety + PID -> software PWM
                                      \
                                       -> telemetry EWMA -> host / Atlas
```

The PID consumes the exact boxcar result before the configurable telemetry
EWMA. Display smoothing therefore cannot delay a cutoff or add hidden phase
lag to the control loop. The callback runs from `adc_stream_task()`, not the
DMA ISR; DMA publication remains bounded and the controller never performs
math in the acquisition interrupt.

The sensor and heater output must be on the same MCU. A controller spanning
two boards would cease to be autonomous and would reintroduce time-sync and
transport failure into its feedback loop.

## Controller form

The first controller is selected with `control: helix_pid` and accepts the
ordinary Klipper `pid_Kp`, `pid_Ki`, and `pid_Kd` values. The host converts
them once to fixed-period Q20 coefficients:

```text
P = Kp * error
I[n] = I[n-1] + Ki * dt * error
D = -Kd / dt * filtered(measurement[n] - measurement[n-1])
output = clamp(P + I + D, 0, max_power)
```

Derivative is taken from the measurement, not the error, so a target change
does not cause derivative kick. `pid_derivative_filter` controls a first-order
low-pass derived at configuration time. Conditional integration accepts an
integrator update only inside the actuator range or when the error drives a
saturated output back toward that range. All runtime arithmetic is integer;
the host rejects coefficients that do not fit the declared representation.

Thermistor conversion remains a host responsibility. With each target the
host uploads the target ADC count and the calibrated local slope around that
target. Far from the target the output normally saturates; near the target the
local linearization supplies the temperature error used by PID. Raw ADC
thresholds remain independent of that approximation.

## Safety contract

PID is never the safety mechanism. The MCU enforces, independently:

1. the configured valid ADC interval;
2. the hard `max_temp` ADC threshold;
3. a sample deadline if DMA/filter delivery stops;
4. `max_power` at the PWM owner;
5. the configured `verify_heater` heating-gain and accumulated-error policy,
   evaluated locally while a target is active;
6. a host-silence transition into an observable autonomous state;
7. an unconditional maximum number of autonomous control samples;
8. output off on firmware shutdown or reset.

Any local safety event latches the controller in `fault` and makes the output
inactive. If the host is present, the asynchronous fault report also shuts
down the printer so motion cannot continue without heat. Clearing requires a
zero host target and an explicit `HEATER_CONTROL_CLEAR`; clearing does not
automatically restore a target. A Klippy instance already in global shutdown
normally uses `FIRMWARE_RESTART`, which rebuilds the controller inactive.

The normal software-PWM object is configured first and then transferred to
the controller. Transfer cancels its timer and queued host updates. This is
essential: two schedulers touching one GPIO can re-energize a heater after a
controller believes it turned the output off.

`failure_policy: hold` and `control: helix_pid` are mutually exclusive. The
former is the older bounded bang-bang takeover used only after host loss; the
latter already owns the loop continuously.

## Host loss and recovery

The host sends a low-rate liveness ping for observability, not to refresh PWM.
After `heater_control_host_timeout`, an active controller changes from
`active` to `autonomous` without changing its target or PID state. After
`heater_control_autonomous_max_duration`, it latches off. A returning host
ping changes a healthy autonomous controller back to `active`.

Manual output is different. It exists for guarded calibration and diagnostic
tests and does not become an unattended fixed-duty heater. The host uploads a
separate manual guard target so the local heating-gain and accumulated-error
policy remains active during autotune without establishing an autonomous PID
target. If the host goes silent during manual mode, the MCU leaves manual mode
and removes output.

A future redundant host must query and adopt controller state without first
resetting the MCU. The current Klippy reconnect path may reset/reconfigure a
board, which intentionally turns its heater off; host redundancy is therefore
an orchestration feature above this controller, not something inferred from
RAM retention.

## Autotune

System identification remains on the host because it is rare, numerical, and
benefits from complete recorded data. During `PID_CALIBRATE`, the host asks the
same MCU controller for guarded manual duty. ADC validity, deadline, ceiling,
and maximum output remain local. The host analyzes the temperature/power
trace, writes the resulting gains to `printer.cfg`, and a restart activates
the new fixed-point profile. Calibration preserves `control: helix_pid`
instead of silently reverting the heater to host PID.

This separation also leaves room for better identification than the original
relay/Ziegler-Nichols method without expanding firmware complexity. Candidate
methods include conservative IMC/SIMC tuning and plant identification for a
later local MPC, but they must be compared on measured overshoot, recovery,
disturbance rejection, and noise sensitivity before becoming defaults.

## Commands and observability

`HEATER_CONTROL_STATUS HEATER=<name>` queries state, fault bits, output,
sample count, locally estimated temperature, and last sample clock.
`HEATER_CONTROL_CLEAR HEATER=<name>` clears a latched fault only with the
target at zero. The ordinary heater status includes an `mcu_control` object so
Mainsail, Moonraker, and Atlas can display whether control is `active`,
`autonomous`, `manual`, or `fault`.

## Qualification gates

- [x] Fixed-point proportional response, bounds, derivative-on-measurement,
  and anti-windup reference tests.
- [x] Host configuration and command encoding against an RP2040 data
  dictionary.
- [x] RP2040 firmware build with ADC DMA and autonomous controller enabled.
- [ ] Cold live configuration on Pico and EBB36 with both outputs confirmed
  inactive and state/sample telemetry advancing.
- [ ] Low-temperature bed step test: compare host PID and MCU PID rise,
  overshoot, settling, duty, and disturbance recovery.
- [ ] Hotend step test at the active material temperature with operator and
  emergency stop present.
- [ ] Host-process loss while holding: target and duty remain bounded until
  reconnection; duration expiry turns output off.
- [ ] ADC stream interruption: sample deadline latches output off.
- [ ] Sensor open/short and ceiling tests with independent temperature
  evidence.
- [ ] `PID_CALIBRATE` guarded-manual run, profile save, restart, and
  coefficient activation.

Until the physical gates pass, `helix_pid` is implemented and workstation-
verified but not the default controller.

## References

* [Kalico PID documentation](https://docs.kalico.gg/PID.html)
* [Kalico MPC documentation](https://docs.kalico.gg/MPC.html)
* [Kalico heater-control source](https://github.com/KalicoCrew/kalico/blob/master/klippy/extras/heaters.py)
* [Kalico MPC source](https://github.com/KalicoCrew/kalico/blob/master/klippy/extras/control_mpc.py)
