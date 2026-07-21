# Predictive Thermal Control

## Purpose

`control: helix_mpc` is a general MCU-autonomous heater controller for thermal
plants whose useful dynamics are slow relative to ADC acquisition. It was
introduced after a real PLA print showed an important distinction:

* the bed temperature was already tightly regulated (0.0717 C settled standard
  deviation and every settled observation within 0.25 C); but
* bed duty varied by about 15.9 percentage points at one standard deviation.

Retuning one Voron bed would not solve the architectural problem. A derivative
term necessarily converts small, quantized temperature changes into output
changes. More filtering can hide that response, but it does not express the
actual control objective: regulate temperature while explicitly minimizing
unnecessary actuator movement.

The complete baseline is retained as
[raw CSV](evidence/heater_control/pla-print-pid-baseline-20260720.csv),
[summary metrics](evidence/heater_control/pla-print-pid-baseline-20260720.json),
and the plot below. The values above use the settled window after the first
five minutes; the linked summary also reports the deliberately more
conservative whole-print window.

![PID print baseline temperature and duty](img/predictive-thermal-pid-baseline.png)

Helix therefore retains `helix_pid` as the compatibility and comparison
controller and adds a separate predictive algorithm. Neither controller is a
safety mechanism; both run beneath the same independent MCU limits, sensor
deadline, heating-progress verification, host-loss bound, and shutdown rules.

## Plant model

The first implementation uses a first-order thermal model:

```text
T(t + H) = Tambient + a * (T(t) - Tambient) + b * u

a = exp(-H / tau)
b = K * (1 - exp(-max(dt, H - L) / tau))
```

`K` is the steady temperature rise at full duty, `tau` is the dominant thermal
time constant, `L` is the fitted dead time, and `H` is the prediction horizon.
The retained free response uses `H`, while the input response uses `H-L`.

The host performs floating point conversion and model fitting. During
development it can also execute the same control law in floating point while
the MCU remains the safety-enforced manual-PWM endpoint. Only after that
physical host loop is accepted are bounded coefficients compared against and
promoted to deterministic fixed-point MCU execution. The MCU never fits a
model or solves a matrix online.

An ambient value may come from a configured temperature object. Otherwise the
host uses the most recent idle observation of the heater itself, then the
explicit fallback. Once uploaded, that value remains usable during host loss.

## Closed-form constrained control

At each local ADC publication, the controller chooses constant horizon duty by
minimizing predicted temperature error and duty movement:

```text
J(u) = (Ttarget - Tpredicted(u))^2
       + rho^2 * (u - uprevious)^2

u_model = (b * (Ttarget - Tfree) + rho^2 * uprevious)
          / (b^2 + rho^2)
```

This is a real scalar model-predictive controller: it predicts a plant state,
optimizes a declared objective, and applies only the first bounded action before
recomputing from the next measurement. Because the plant has one input and the
objective is quadratic, the optimum has a closed form. A general online QP
solver would add complexity without improving this control problem.

Three supporting mechanisms make the model robust:

1. An MCU-local observer filters the temperature used for control. Raw ADC
   values still feed safety checks without this delay.
2. A slow signed integral bias learns unmodelled heat loss and ambient error.
   Directional anti-windup respects both the hard output range and slew bound.
3. An independent output slew limit bounds every duty transition even if a
   model or target changes. Model updates rebase the bias around the current
   output for a bumpless transition.

There is no derivative of a quantized sample. The movement penalty directly
states how much temperature correction is worth a change in duty.

The current MCU thermistor representation is a target-local tangent, not a
global nonlinear conversion. Prediction is therefore restricted to the
configured `thermal_control_band` around the target. Outside that band the MCU
uses slew-bounded full/off approach control, clears the observer, and enters
the model bumplessly only after the local conversion is valid. Safety always
uses raw ADC thresholds.

## Characterization and scheduling

The guarded command below performs an off-state drift preflight followed by a
constant-power step:

```text
HELIX_THERMAL_MODEL_CALIBRATE HEATER=heater_bed \
  TARGET=60 POWER=0.5 DURATION=900 CEILING=80 CONFIRM=YES
```

The physical output remains owned by the MCU. Its temperature ceiling, ADC
range, sample deadline, maximum power, heating-progress verification, and
host-loss behavior remain active. The host rejects a run when:

* fewer than 30 finite samples were captured;
* the observation is shorter than 10 seconds;
* off-state drift exceeds the configured limit;
* heating produces less than a 2 C rise;
* the best time constant is pinned to the fit search boundary; or
* a first-order response explains less than 95 percent of measured variance.

Accepted fits are stored as candidates, never activated automatically:

```text
HELIX_THERMAL_MODEL_STATUS HEATER=heater_bed
HELIX_THERMAL_MODEL_VALIDATE HEATER=heater_bed ID=<id> STATUS=VALIDATED CONFIRM=YES
HELIX_THERMAL_MODEL_COEFFICIENTS HEATER=heater_bed
HELIX_THERMAL_MODEL_CLEAR HEATER=heater_bed CONFIRM=YES
```

Validated models may be interpolated between measured target temperatures.
Extrapolation is forbidden. Gain and time constant are bounded relative to the
explicit `printer.cfg` model, and dead time has an independent absolute bound,
before upload. A malformed store, rejected run, out-of-range target, time
constant faster than the control period, or dead time beyond the configured
prediction horizon falls back to the explicit model.

## Generality

The algorithm does not contain bed-specific constants. The same controller and
fixed-point code cover a high-inertia bed and a low-inertia hotend; only the
identified plant and policy parameters differ. Optional local disturbance
inputs—part-cooling fan duty and extrusion heat flow—are a future model
extension, not a prerequisite for autonomous operation.

Workstation simulation currently qualifies representative plants with:

| Plant | Gain | Time constant | Dead time | Target |
|---|---:|---:|---:|---:|
| Bed | 90 C/duty | 300 s | 2.0 s | 55 C |
| Hotend | 280 C/duty | 20 s | 0.6 s | 210 C |

Both simulations include 0.05 C quantization. The hotend case also includes a
sustained load disturbance. After five time constants, both remain below
0.15 C mean error, 0.20 C standard deviation, 0.60 C peak error, and 0.02 RMS
duty change per controller update. These are deterministic regression gates,
not substitutes for physical qualification.

The first physical MCU feasibility run on 2026-07-20 used the V0 bed at 55 C.
It had no fault samples, 0.25 C overshoot, and very smooth steady duty
(`0.00190` RMS update delta), but required 206.95 seconds to become
print-ready. The controller did not reach its first target crossing until
298.63 seconds. That is useful feasibility evidence, but it is not an
acceptance pass: MCU execution happened before the host-first tuning gate, and
the run was not paired with PID under identical initial conditions. The
[raw capture](evidence/heater_control/mcu-predictive-bed55-feasibility-20260720.csv)
and [summary](evidence/heater_control/mcu-predictive-bed55-feasibility-20260720.json)
are retained specifically so the slow warm-up is not mistaken for success.

## Host-first development gate

For a low-speed plant such as the bed, predictive-controller iteration follows
this order:

1. Run the floating-point predictive loop in Klippy against the physical bed;
   the MCU applies requested duty only through its local ADC, ceiling,
   watchdog, and fault guards.
2. Tune and accept the host loop, including time-to-print. Simulation and
   replay are supporting tools, not a substitute for this physical run.
3. Replay the accepted observations through both floating-point and MCU
   fixed-point implementations and bound their duty/output error.
4. Enable MCU execution once, then repeat thermal performance and host-loss
   safety gates. Do not use repeated firmware heat cycles as the tuning loop.

The first host candidate deliberately does not rebase the model-error bias when
ordinary full-power approach enters the predictive band. That handoff is not a
model change, and inherited full-power bias was the principal cause of the
206.95-second feasibility result. The independent duty slew limit already
bounds the transition. Bumpless bias rebase remains required for an actual
model/profile change; the MCU implementation is not changed until this host
candidate passes physically.

The first 75 C open-printer host run found a second handoff defect: a stateless
1 C boundary repeatedly alternated between full approach power and predictive
duty as the bed crossed 74 C. The run was stopped with zero faults rather than
allowing boundary chatter to masquerade as settling. The replacement is a
continuous cross-fade: prediction owns duty inside the configured band,
approach owns it outside twice the band, and their requested duties are mixed
linearly between those boundaries before the independent slew clamp. This
removes both mode chatter and a discrete hysteresis transition.

The rerun passed physically on 2026-07-20 with the printer open, a 75 C target,
28.09 C ambient, and therefore a 46.91 C target-to-ambient excitation. From a
51.92 C warm start it became print-ready in 51.62 seconds, then remained inside
+/-1 C for the required 60 seconds. Overshoot was 0.24 C, steady temperature
standard deviation was 0.235 C, RMS duty change was 0.00152, and there were no
fault samples. This accepts the floating-point host candidate for fixed-point
promotion; it does not close the separately required paired PID comparison or
closed-enclosure 110 C characterization. The
[accepted trace](evidence/heater_control/host-predictive-bed75-open-blend-20260720.csv),
[accepted summary](evidence/heater_control/host-predictive-bed75-open-blend-20260720.json),
[rejected hard-boundary trace](evidence/heater_control/host-predictive-bed75-open-hard-boundary-failed-20260720.csv),
and [rejection record](evidence/heater_control/host-predictive-bed75-open-hard-boundary-failed-20260720.json)
preserve the complete decision.

The recorded thermal envelope was resampled at the 0.3-second control period
and replayed through the accepted floating-point law and the real compiled C
fixed-point arithmetic. Maximum duty disagreement was 0.0001746 and mean
disagreement was 0.0000309. That parity result clears promotion to a single MCU
physical confirmation; it is not permission to skip the remaining safety
injection gates.

That MCU confirmation passed on the flashed RP2040 at the same 75 C target and
open-printer condition. From 50.43 C it became print-ready in 56.13 seconds,
overshot 0.12 C, held the complete 60-second band, showed 0.227 C steady
standard deviation and 0.00178 RMS duty change, and reported no faults. The
host began 1.49 C warmer; normalizing time-to-readiness by the required
temperature rise gives 2.338 s/C for host and 2.381 s/C for MCU, a 1.8-percent
difference. The [MCU trace](evidence/heater_control/mcu-predictive-bed75-open-blend-20260720.csv),
[MCU summary](evidence/heater_control/mcu-predictive-bed75-open-blend-20260720.json),
and [host/MCU comparison](evidence/heater_control/bed75-open-host-mcu-comparison-20260720.json)
close the promotion confirmation. Production remains `helix_pid` until the
same-target paired PID and injected safety gates pass.

`HELIX_HEATER_CONTROL_MODE HEATER=<name> MODE=HOST TARGET=<C> CONFIRM=YES`
selects the floating-point predictive law for `control: helix_mpc` and ordinary
host PID for `control: helix_pid`. The target and output must be zero during a
mode transition.

## Physical qualification gates

The predictive controller is not the preferred production controller until a
paired physical experiment passes all of the following:

1. Identical target, ambient, fan, material-flow, and measurement conditions
   for `helix_pid` and `helix_mpc`.
2. Temperature RMS and peak error no worse than PID.
3. At least 50 percent reduction in RMS duty change or a clearly explained
   physical limit.
4. Time-to-print is measured from the target command to the first entry into a
   +/-1 C band that remains uninterrupted for 60 seconds. It must be no slower
   than the paired PID baseline by more than 5 percent; improvement is the
   optimization goal. First crossing, overshoot, and step-down recovery are
   reported separately so a fast but oscillatory crossing cannot pass.
5. No output saturation during the intended operating envelope.
6. Host-loss continuation, stale-sample cutoff, maximum-temperature cutoff,
   and firmware restart behave identically to the qualified PID safety path.

The goal is not to declare model-based control superior by construction. The
goal is to make the objective measurable and retain the new algorithm only if
the installed hardware confirms the expected advantage.

`scripts/helix_heater_qualification.py` refuses a configured warm start,
records the controller/model identity in every raw row, and writes summary
metrics. `scripts/helix_heater_compare.py` then checks target/readiness
identity, initial temperature within 1 C, the 5-percent time-to-print bound,
temperature RMS/peak error, at least 50-percent duty-delta reduction,
overshoot, and zero faults. A passing comparison is necessary but does not
replace the separate step-down and host-loss safety gates.
