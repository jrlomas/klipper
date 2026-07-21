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

## Predictive controller extension

`control: helix_mpc` is the second controller under this same ownership and
safety contract. It is not a renamed PID tune. The host characterizes a
first-order-plus-dead-time plant (`gain`, `tau`, `delay`), selects a bounded
model for the requested target, and converts the prediction horizon to fixed
coefficients. The MCU
uses a closed-form scalar receding-horizon solution:

```text
Tfree = Tambient + a * (Tfiltered - Tambient)
u = argmin ((Ttarget - Tfree - b*u)^2 + rho^2*(u-uprevious)^2)
```

A slow signed integral bias rejects model error; it uses directional
anti-windup against both output saturation and the independent duty-slew
bound. Model changes rebase that bias around the current duty. Raw ADC safety
never consumes the observer-filtered temperature.

The model store follows the PID profile rules: immutable candidate evidence,
explicit validation, bounded interpolation only inside characterized targets,
no extrapolation, atomic private persistence, and explicit clearing. Guarded
step characterization executes through the MCU manual owner so local ceiling,
deadline, maximum-power, and heating-progress enforcement remain active.
Details, equations, commands, and physical acceptance gates are in
[Predictive Thermal Control](../../Predictive_Thermal_Control.md).

## Safety contract

PID is never the safety mechanism. The MCU enforces, independently:

1. the configured valid ADC interval;
2. the hard `max_temp` ADC threshold;
3. a sample deadline if DMA/filter delivery stops while the controller is
   active, autonomous, or in guarded-manual mode; the deadline is disarmed in
   inactive `ready` state because there is no output to cut off;
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
The firmware measures host silence from the controller callback's current MCU
execution clock, not from a possibly buffered acquisition timestamp. After
`heater_control_host_timeout`, an active controller changes from
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
trace. A single tune may update the base `printer.cfg` gains, but Helix also
stores its evidence as a candidate in an atomic, versioned host registry.
Candidate gains are not activated merely because an autotune completed.
Calibration preserves `control: helix_pid` instead of silently reverting the
heater to host PID.

The original Klipper relay drives full power and uses a fixed peak count.
Kalico/Danger-Klipper improves the experiment by adapting the high-side power
of a zero-to-power relay. Helix retains both as `METHOD=LEGACY` and
`METHOD=ADAPTIVE`, but the one-sided adaptive method failed its 0.02 convergence
gate at the physically important 200 C hotend point after all 60 peaks. Near
the upper rail, one number controlled both equilibrium bias and excitation
amplitude, making convergence unnecessarily sensitive.

`METHOD=SYMMETRIC` is therefore the qualified `helix_pid` default. The existing
controller first holds the target for a configurable settling window and
averages the required duty. Identification alternates `B-Delta` and
`B+Delta`; midpoint and heating/cooling duration errors adapt `B`, while the
measured oscillation adapts `Delta`. Both legs retain rail margin. Ku uses the
actual symmetric relay amplitude, `4*Delta/(pi*a)`. The 260 C physical run
converged in seven cycles with `B=0.6984`, `Delta=0.0896`, `Ku=0.15485`, and
`Tu=16.0004 s`. Helix records biases, deltas, half-cycle timing, peaks, and
extrema, and offers classic Ziegler-Nichols (`RULE=ZN`) plus conservative
Tyreus-Luyben (`RULE=TL`) conversion. A completed tune remains an inactive
candidate until explicit validation.

The hotend qualification target is 260 C going forward. Lower-temperature
100 C and 200 C records remain useful developmental baselines, but they do not
substitute for release evidence at the intended ABS operating point.

This separation also leaves room for better identification than the original
relay/Ziegler-Nichols method without expanding firmware complexity. Candidate
methods include conservative IMC/SIMC tuning and plant identification for a
later local MPC, but they must be compared on measured overshoot, recovery,
disturbance rejection, and noise sensitivity before becoming defaults.

## Characterization registry and gain model

Every run records heater, target, optional context temperature, method,
firmware identity, gains, extrema, duration, relay powers, and peak evidence.
Its state is `candidate`, `validated`, or `rejected`. Only validated points
participate in scheduling.

With target temperature alone, Kp, Ki, and Kd are three piecewise-linear
curves—not a three-dimensional surface. Exact points are used directly and
intermediate targets interpolate between adjacent points. With an explicitly
configured ambient/chamber sensor and at least three non-collinear validated
points, each gain may instead use a bounded plane:

```text
gain = a + b*target + c*context_temperature
```

The model never extrapolates beyond observed target/context ranges and clamps
every result to configured ratios around the base gains. Missing context,
insufficient points, invalid data, or an out-of-hull request falls back to the
base profile. The host uploads one selected gain set; the MCU does not evaluate
an unconstrained model in its safety loop. Gain changes retain the prior output
contribution and clear derivative history, providing a bumpless transition
before the new target is applied.

Management is deliberately explicit. Status and coefficient commands are
read-only. Validation/rejection and clearing require confirmation. A retrain
runs an ascending target list, preserves old data if any tune fails, and only
replaces old records after the complete sequence succeeds. Its new runs still
require validation.

Physical registry qualification includes validated bed 60 C and hotend 100,
120, and 260 C points. Candidate inactivity, validation, exact selection,
100-to-120 C linear interpolation, restart persistence, no-extrapolation base
fallback, and raw-versus-bounded status were observed live. At 260 C the raw
TL profile `17.948/0.510/45.583` was selected exactly; the configured 0.25x
base floor bounded Ki to 2.03725 and exposed that clamp before activation.
Context-surface and held bumpless-transition qualification remain open.

## Oversampling, dither, and effective resolution

ADC oversampling and actuator dithering are separate. For uncorrelated noise,
N conversions can ideally add `0.5*log2(N)` effective bits; 128x therefore has
a 3.5-bit ceiling. A 12-bit accumulator shifted by three can retain a 16-bit
code representation (0..65520), but it cannot claim more than 15.5 ideal ENOB
and will usually achieve less. The default shift returns native 12-bit scale
for compatibility; retained-bit mode is opt-in and scales sensor conversion,
target, and local safety thresholds together.

Natural ADC/front-end noise may already provide useful sub-LSB dither. Helix
does not add deliberate dither until raw-code histograms show stuck codes and
an experiment proves a benefit. It reports raw-code standard deviation, code
occupancy, peak-to-peak noise, lag-one correlation, and a gain-versus-OSR
curve. A stuck code cannot establish resolution because it supplies no
sub-code information. A sine
fixture additionally reports residual SINAD/ENOB. Correlated reference drift,
settling error, INL, and DNL cannot be averaged away.

The software PWM already maps 16-bit duty into millions of timer ticks at the
normal heater cycle, so output quantization is not presently limiting. Pulse-
density or sigma-delta heater dithering would add switching and spectral
energy without evidence of benefit and remains disabled pending measurement.

The installed system also admits a useful test without an external waveform
source. After PID stabilization, `HELIX_HEATER_SINE_TEST` applies a slow,
biased open-loop PWM sine under an MCU-local temperature ceiling. A least-
squares fit absorbs the thermal plant's amplitude attenuation and phase lag;
all remaining harmonics, drift, noise, airflow, sensor error, and ADC error
form the residual. The resulting SINAD is intentionally called thermal-chain
SINAD or effective control resolution. It answers how well the real installed
system follows a known excitation, but cannot isolate ADC ENOB because the
heater, mechanics, and thermistor are inside the measurement path.

AUTO bias is a settled-window average, not the instantaneous output at first
setpoint entry. Explicit bias retains the same settling phase and logs its
difference from the independently measured value. The host controller also
terminates at the manual ceiling or a cleared target, in addition to the MCU's
independent guard. These requirements were added after a rejected 260 C run
sampled transient duty and drifted to 272.6 C. Corrected 260 C runs at 30 and
60 s periods measured 12.156 and 28.843 C/duty, respectively, with faults and
EBB transport errors remaining zero. The 2.37x gain increase at the longer
period is the expected installed thermal low-pass response.

## Commands and observability

Klippy explicitly queries controller state and MCU-measured loop timing once
per second. This avoids streaming ordinary control samples over the transport
while still making state, fault bits, output, sample count, locally estimated
temperature, last sample clock, and control-cadence mean/standard deviation
visible through ordinary heater status. A distinct unsolicited fault event is
reserved for a local safety trip so the host does not wait for the next
telemetry query before shutting down coordinated motion.

The MCU converts ADC error with a tangent linearized at the current target;
that estimate is useful to the local controller near its setpoint but is not a
global thermistor conversion. Status therefore exposes the raw
`mcu_temperature_estimate` plus `mcu_temperature_valid`; `mcu_temperature` is
`null` while idle or more than 5 C from the tangent's target. The ordinary
heater `temperature` remains the full host-side sensor conversion.

`HEATER_CONTROL_STATUS HEATER=<name>` performs an immediate state and timing
query and reports the same fields.
`HEATER_CONTROL_CLEAR HEATER=<name>` clears a latched fault only with the
target at zero. The ordinary heater status includes an `mcu_control` object so
Mainsail, Moonraker, and Atlas can display whether control is `active`,
`autonomous`, `manual`, or `fault`. It also reports raw selected gains, applied
bounded gains, the names of clamped terms, and whether execution is on the MCU
or in guarded host-comparison mode.

`HELIX_HEATER_CONTROL_MODE HEATER=<name> MODE=HOST TARGET=<C> CONFIRM=YES`
creates ordinary Klippy `ControlPID` for `helix_pid`, or the floating-point
predictive reference for `helix_mpc`, while keeping MCU-local manual
temperature, ADC, watchdog, and ceiling guards. `MODE=MCU CONFIRM=YES` restores
autonomous execution. Both changes require target and output zero; the command
exists for controlled qualification, not automatic failover. Bed-controller
development must physically accept the host loop—including time-to-print—then
pass host/fixed-point parity before MCU execution is a release candidate.

`HELIX_PID_PROFILE_STATUS`, `HELIX_PID_PROFILE_COEFFICIENTS`,
`HELIX_PID_PROFILE_VALIDATE`, `HELIX_PID_PROFILE_CLEAR`, and
`HELIX_PID_PROFILE_RETRAIN` expose the characterization lifecycle. Destructive
commands require `CONFIRM=YES`.

## Qualification gates

- [x] Fixed-point proportional response, bounds, derivative-on-measurement,
  and anti-windup reference tests.
- [x] Fixed-point predictive response, observer, explicit duty-movement cost,
  signed model-error correction, anti-windup, slew bounds, and bumpless model
  reconfiguration tests.
- [x] Persistent candidate/validated thermal models with bounded target
  interpolation and no extrapolation.
- [x] Guarded step fitting rejects drift, weak excitation, poor first-order
  fit, and an underidentified time constant.
- [x] Host configuration and command encoding against an RP2040 data
  dictionary.
- [x] RP2040 firmware build with ADC DMA and autonomous controller enabled.
- [x] Cold live configuration on Pico and EBB36 with both outputs confirmed
  inactive and state/sample telemetry advancing.
- [x] Low-temperature bed step test: compare host PID and MCU PID rise,
  overshoot, settling, duty, and disturbance recovery.
- [x] Hotend step tests at 100 C and the release target of 260 C with
  operator and emergency stop present; the 260 C final-minute variation was
  0.09 C peak-to-peak and 0.0223 C standard deviation.
- [x] Host-process loss while holding: target and duty remain bounded until
  reconnection and state transitions active/autonomous/active.
- [ ] Autonomous-duration expiry turns output off.
- [ ] ADC stream interruption: sample deadline latches output off.
- [ ] Sensor open/short and ceiling tests with independent temperature
  evidence.
- [x] Legacy `PID_CALIBRATE` guarded-manual run completes with target zero,
  local safety active, and finite coefficients.
- [x] Candidate storage, inactivity before validation, explicit validation,
  exact and interpolated activation, restart persistence, fallback, and
  raw/bounded gain visibility. Symmetric tuning passed at 260 C after the
  one-sided adaptive method failed at 200 C.
- [ ] Context-surface/convex-hull and held bumpless-transition qualification.
- [ ] Raw 1x..128x DC/sine capture establishes measured ENOB, correlation,
  useful dither, and the retained-bit shift limit.
- [x] Guarded PWM-sine runs at 260 C and 30/60 s periods establish installed
  thermal-chain gain, phase, drift, residual, and effective control resolution.
- [ ] Paired physical `helix_pid` versus `helix_mpc` qualification on the bed.

`helix_pid` has passed nominal heated operation, physical printing, and
host-loss continuity; the remaining injected cutoff cases stay explicit above.
`helix_mpc` has workstation qualification plus one unpaired physical MCU
feasibility capture. That capture was intentionally not accepted because its
time-to-print was 206.95 seconds and it bypassed the host-first tuning gate; it
must not replace the production controller until the physical host loop and
paired PID comparison pass.

## References

* [Kalico PID documentation](https://docs.kalico.gg/PID.html)
* [Kalico MPC documentation](https://docs.kalico.gg/MPC.html)
* [Kalico heater-control source](https://github.com/KalicoCrew/kalico/blob/master/klippy/extras/heaters.py)
* [Kalico MPC source](https://github.com/KalicoCrew/kalico/blob/master/klippy/extras/control_mpc.py)
