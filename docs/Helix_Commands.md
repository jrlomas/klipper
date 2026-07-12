# HELIX Command &amp; Feature Reference

Every G-code command, config section, and firmware capability HELIX adds
on top of Klipper, in one place. Each command links to its full
description in the [G-Code reference](G-Codes.md); each config option to
the [Config Reference](Config_Reference.md). The inherited Klipper
commands are unchanged and documented in those same references.

Everything here is **opt-in** — the relevant config section or per-object
option must be present, and (for firmware capabilities) the board's
firmware must be built with the matching Kconfig flag. Ask any running
machine what it actually has with **`HELIX_STATUS`**.

## Console commands

### Trajectory motion — `[trajectory_queuing]`
| Command | Summary |
| --- | --- |
| `TRAJECTORY_STATUS` | State of every trajectory actuator — anchored / needs-rebase, commanded position, sub-unit resolution, and whether the firmware supports higher-order segments. |
| `BEZIER_MOVE STEPPER=<name> DURATION=<s> P0=.. P1=.. P2=.. P3=.. [P4=.. P5=..]` | Advanced/commissioning: drive one trajectory joint along a cubic (4 points) or quintic (6 points) Bézier. Opt-in via `enable_bezier_move`; idle-only; bypasses kinematics (follow with `SET_KINEMATIC_POSITION`). |

### Failure recovery — `[failure_recovery]`
| Command | Summary |
| --- | --- |
| `FAILURE_RECOVERY_STATUS` | Configured heater holds (policy, ceiling, duration, engaged?) and any micro-controllers in the paused-link state. |
| `RECONNECT_MCU MCU=<name>` | Re-handshake a board that lost its link and entered pause-and-hold. |
| `RESUME_MOTION` | Reconcile every joint from its execution log and resume the print; blocks only for a joint whose homing was genuinely lost. |
| `ENGAGE_HEATER_HOLD [HEATER=<name>]` | Manually engage the autonomous heater failsafe hold. |
| `RELEASE_HEATER_HOLD [HEATER=<name>]` | Release the hold and return the heater(s) to host control. |
| `EXECLOG_DUMP` | Drain the retained micro-controller execution logs (the "flight recorder") to the Klipper log. |

### Machine time — `[timesync]`
| Command | Summary |
| --- | --- |
| `TIMESYNC_STATUS` | Per-secondary beacon-discipline state: converged?, current sync error (µs), clock-rate correction (ppm). |

### Capability introspection — `[helix_status]` (auto-loaded)
| Command | Summary |
| --- | --- |
| `HELIX_STATUS` | Which HELIX firmware features each MCU was built with (read from its served dictionary) and which host subsystems are loaded. The fastest answer to "what does this printer support, and what's on?" |

## Config surface (new)

### Per-stepper (in any `[stepper_*]` / rail)
| Option | Summary |
| --- | --- |
| `motion_protocol: trajectory` | Opt this actuator into the trajectory-intention path (default `legacy`). The options below apply only then. |
| `motion_tolerance`, `motion_sample_time` | Host segment-fitter fidelity. |
| `motion_underrun_decel` | Deceleration used to ramp to a safe stop if the segment queue underruns. |
| `motion_homing_volatile` | `True` marks a joint whose homing cannot be trusted across a board reset — it must be re-homed before a resume. Default `False` (assume the last commanded position + prior homing). |

### Sections
| Section | Summary |
| --- | --- |
| `[trajectory_queuing]` | Owns trajectory actuators; `enable_bezier_move: True` enables `BEZIER_MOVE`. Usually auto-loaded. |
| `[failure_recovery]` | Enables pause-and-hold and the recovery commands. |
| `[helix_status]` | Enables `HELIX_STATUS` (also auto-loaded with the trajectory subsystem). |
| `[timesync]` | Machine-time beacon discipline (`beacon_interval`, `freewheel_time`, `converge_window`). |
| `[asyncio_bridge]` | The asyncio↔reactor seam (`start_timeout`, `stop_timeout`). |
| `[mcu] on_comm_timeout: pause` | Turn a lost link on a secondary MCU into pause-and-hold instead of shutdown. |
| `[mcu] hardware_endstop_trigger: False` | Force the legacy polled endstop path on a board (default: use hardware edge interrupts when the firmware supports them). |
| `[heater_*] failure_policy: hold` | Keep a heater at its target through a fault (`hold_max_temp`, `hold_max_duration`). |

## Firmware capabilities (Kconfig)

Built into the micro-controller image and advertised in its data
dictionary. `HELIX_STATUS` reports which a given board has. Most default
on where code size allows and off on `HAVE_LIMITED_CODE_SIZE` boards.

| Flag | Capability |
| --- | --- |
| `WANT_TRAJECTORY` | The trajectory segment core (intentions, drift-free chaining, underrun ramps). |
| `WANT_TRAJECTORY_HIGHER_ORDER` | Cubic/quintic (jerk- and snap-limited) Bézier segments. |
| `WANT_TRAJECTORY_PWM` | The sampled PWM/DAC actuator backend (a non-stepper actuator — the same door a future BLDC/FOC backend uses). |
| `WANT_TRIGGER_SOURCE` | Hardware-event trigger sources — edge interrupts, comparators, ADC watchdogs, input capture. |
| `WANT_HEATER_HOLD` | The autonomous heater failsafe hold. |
| `WANT_SYSCALL_API` | The unified cross-family board syscall table (advertised as `BOARD_SYSCALL_ABI`/`CAPS`). |
| `WANT_SIGNED_IMAGES` | Ed25519 signature verification of firmware images in the bootloader (where it fits). |

## MCU-level commands (low level)

Ordinarily driven by the host, not typed by hand — documented in
[MCU_Commands.md](MCU_Commands.md). Highlights: `config_traj_stepper` /
`queue_traj_segment` / `queue_traj_segment_cubic` /
`queue_traj_segment_quintic` (motion), `config_traj_pwm` (sampled
backend), `config_trigger_gpio` / `trigger_source_arm` (hardware
triggers), `config_heater_hold` (failsafe), `config_execlog` (flight
recorder), and `query_board_syscalls` (capability negotiation).

---

For the narrative behind any of these, see the [HELIX overview](HELIX.md),
the [User Guide](Helix_User_Guide.md), and the design canon in
[RFC 0001](rfcs/0001-motion-intentions/00-Vision.md).
