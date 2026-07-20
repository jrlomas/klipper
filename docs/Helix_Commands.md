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

Terms like *segment*, *execution log*, or *framing v2* are defined in the
[glossary](Glossary.md).

## Console commands

### Trajectory motion — `[trajectory_queuing]`
| Command | Summary |
| --- | --- |
| `TRAJECTORY_STATUS` | State of every trajectory actuator — anchored / needs-rebase, commanded position, sub-unit resolution, and whether the firmware supports higher-order segments. |
| `BEZIER_MOVE STEPPER=<name> DURATION=<s> P0=.. P1=.. P2=.. P3=.. [P4=.. P5=..]` | Advanced/commissioning: drive one trajectory joint along a cubic (4 points) or quintic (6 points). Opt-in via `enable_bezier_move`; idle-only; bypasses kinematics (requires `[force_move] enable_force_move: True`, then follow with `SET_KINEMATIC_POSITION`). Long curves are split into fixed-point-safe, half-microstep-faithful wire segments; same-MCU periodic TMC checks pause only during the move and resume immediately afterward. |

### Failure recovery — `[failure_recovery]`
| Command | Summary |
| --- | --- |
| `FAILURE_RECOVERY_STATUS` | Configured heater holds (policy, ceiling, duration, engaged?) and any micro-controllers in the paused-link state. |
| `RECONNECT_MCU MCU=<name>` | Re-handshake a board that lost its link and entered pause-and-hold. |
| `RESUME_MOTION` | Reconcile every joint from its execution log and resume the print; blocks only for a joint whose homing was genuinely lost. During a trajectory recovery hold, ordinary `RESUME` and the Mainsail resume action automatically route here. |
| `ENGAGE_HEATER_HOLD [HEATER=<name>]` | Manually engage the autonomous heater failsafe hold. |
| `RELEASE_HEATER_HOLD [HEATER=<name>]` | Release the hold and return the heater(s) to host control. |
| `EXECLOG_DUMP` | Drain the retained micro-controller execution logs (the "flight recorder") to the Klipper log. |

### Machine time — `[timesync]`
| Command | Summary |
| --- | --- |
| `TIMESYNC_STATUS` | Per-secondary beacon-discipline state: converged?, current sync error (µs), clock-rate correction (ppm). |
| `QUERY_PIN_TIMING PIN=<name>` | Commissioning readback of each target MCU's scheduled and actual diagnostic GPIO-write clocks. |
| `SET_PIN_LEGACY_TIMING PIN=<name> VALUE=<0\|1>` | On a machine-time output, schedule one comparator edge through original Klipper per-MCU `print_time` conversion for scope comparison. |
| `SYNC_LINE_TEST [SAMPLES=<count>]` | With `[machine_time_sync_line]`, compare a direct primary-to-secondary edge timestamp with the current USB clock map. |
| `USB_SOF_TEST [SAMPLES=<count>]` | With `[usb_sof_sync]`, match USB frame timestamps across two MCUs and calibrate them against the direct sync line. |

### CAN transport — `[helix_can <bus>]`
| Command | Summary |
| --- | --- |
| `HELIX_CAN_STATUS BUS=<bus>` | Active profile and rates, transaction/time epochs, required nodes, per-node FIFO/protocol errors, retries, queue occupancy, and accepted-to-forwarded delivery accounting. Counters are cumulative; compare deltas when diagnosing a particular print. |
| `HELIX_CAN_QUIESCE BUS=<bus> [PROFILE=<classic-profile>]` | Drain motion and place the bus on an allowlisted Classical CAN maintenance profile before stopping Klipper to flash a bridge or node. |

### MCU-autonomous heater control — `control: helix_pid`
| Command | Summary |
| --- | --- |
| `HEATER_CONTROL_STATUS HEATER=<name>` | Query local state, fault, output, sample count, temperature, and loop cadence. |
| `HEATER_CONTROL_CLEAR HEATER=<name>` | Clear a latched local heater fault with target zero. |
| `HELIX_HEATER_CONTROL_MODE HEATER=<name> MODE=<HOST\|MCU> [TARGET=<C>] CONFIRM=YES` | Enter guarded host comparison mode with the same bounded gains, or restore autonomous MCU control; target and output must be zero during either transition. |
| `HELIX_PID_PROFILE_STATUS HEATER=<name>` | List candidate, validated, and rejected characterization runs. |
| `HELIX_PID_PROFILE_COEFFICIENTS HEATER=<name>` | Show bounded curve or target/context surface coefficients and measured hull. |
| `HELIX_PID_PROFILE_VALIDATE HEATER=<name> RUN=<id> STATUS=<VALIDATED\|REJECTED> CONFIRM=YES` | Change a candidate's explicit validation state. |
| `HELIX_PID_PROFILE_CLEAR HEATER=<name> CONFIRM=YES` | Clear one heater's stored characterization registry. |
| `HELIX_PID_PROFILE_RETRAIN HEATER=<name> TARGETS=<t1,t2,...>` | Run ascending symmetric relay tunes without changing the base profile; every result remains inactive until validation. |
| `HELIX_HEATER_SINE_TEST HEATER=<name> CENTER=<C> CEILING=<C> [SETTLE_TIME=<s>]` | Settle and measure holding duty, then apply a host- and MCU-guarded PWM sine and report installed thermal-chain gain, phase, residual, and SINAD. |

### Structured trace — `[atlas_trace]`
| Command | Summary |
| --- | --- |
| `ATLAS_TRACE_STATUS` | Per-MCU trace availability, received records, sequence gaps, ring bounds, and explicit drop counts. |
| `ATLAS_TRACE_LEVEL MCU=<name> SUB=<subsystem> LEVEL=<level>` | Set one subsystem's trace threshold. |
| `ATLAS_TRACE_STREAM MCU=<name> MAX=<records>` | Set the records-per-task-wake streaming budget; zero disables streaming. |
| `ATLAS_TRACE_TEST MCU=<name> [COUNT=<records>]` | Emit bounded registered commissioning records through the real ring/streamer without motion. |

### Capability introspection — `[helix_status]` (auto-loaded)
| Command | Summary |
| --- | --- |
| `HELIX_STATUS` | Which HELIX firmware features each MCU was built with (read from its served dictionary), its protocol/ABI hash, the live fleet verdict/action, and which host subsystems are loaded. The fastest answer to "what does this printer support, and is it in lockstep?" |

### Built-in self test — `[helix_self_test]`
| Command | Summary |
| --- | --- |
| `HELIX_SELF_TEST [MCU=<name>]` | Run each board's live verification gates through the protocol — wire CRC vector, timer monotonicity, RAM pattern, and the trajectory fixed-point kernel against host golden vectors — plus a link round-trip measurement. The verification stage as a console command; once green, a field diagnostic. `on_connect`/`required` options run it automatically at connect. |

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
| `[trajectory_pwm <name>]` | Sampled PWM/DAC trajectory actuator; supports direct segments and a bounded, preflighted scalar value-function feed. |
| `[failure_recovery]` | Enables pause-and-hold and the recovery commands. |
| `[helix_status]` | Enables `HELIX_STATUS` (also auto-loaded with the trajectory subsystem). |
| `[timesync]` | Machine-time beacon discipline (`beacon_interval`, `freewheel_time`, `converge_window`); `usb_sof: True` uses matching USB hardware-frame timestamps when supported. |
| `[output_pin <name>] machine_time: True` | Schedule a digital edge in the primary MCU's machine-time domain; combine with `[multi_pin]` for a synchronized cross-board output. |
| `[machine_time_sync_line]` | Commissioning-only direct wire from a primary output to a passive secondary edge timestamp. |
| `[usb_sof_sync]` | Commissioning-only matching USB Start-of-Frame timestamps, calibrated against `[machine_time_sync_line]`. |
| `[atlas_trace]` | Structured per-MCU trace collection, subsystem levels, bounded streaming, and drop accounting. |
| `[asyncio_bridge]` | The asyncio↔reactor seam (`start_timeout`, `stop_timeout`). |
| `[helix_self_test]` | Built-in test mode: `HELIX_SELF_TEST` plus `on_connect`/`required` to run the boards' live verification gates at every connect. |
| `[intentproto_transport NAME]` | The v2 transport bridge: klippy speaks intentproto v2 (auth + FEC envelope around stock v1 frames) to a network (`mode: datagram`) or serial (`mode: bch`) board; point `[mcu NAME] serial:` at its PTY. |
| `[mcu] on_comm_timeout: pause` | Turn a lost link on a secondary MCU into pause-and-hold instead of shutdown. |
| `[mcu] hardware_endstop_trigger: False` | Force the legacy polled endstop path on a board (default: use hardware edge interrupts when the firmware supports them). |
| `[mcu] hardware_endstop_observer: True` | Commissioning only: timestamp GPIO edges through a passive ISR while the legacy poller remains the stop owner. |
| `[heater_*] failure_policy: hold` | Keep a heater at its target through a fault (`hold_max_temp`, `hold_max_duration`). |
| `[heater_*] control: helix_pid` | Run PID and safety on the heater-owning MCU, with bounded validated gain scheduling and guarded system-identification tests. |

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
| `WANT_HEATER_CONTROL` | MCU-local PID, safety, manual-test ceiling, dynamic profiles, and control telemetry. |
| `WANT_SYSCALL_API` | The unified cross-family board syscall table (advertised as `BOARD_SYSCALL_ABI`/`CAPS`). |
| `WANT_SIGNED_IMAGES` | Ed25519 signature verification of firmware images in the bootloader (where it fits). |
| `WANT_SELF_TEST` | The built-in self-test commands (`run_self_test`): the live verification gates / diagnostics driven by `HELIX_SELF_TEST`. |
| `WANT_CONSOLE_FRAMING_V2` | Accept intentproto v2 (BCH FEC) framing on the serial UART console (a framing transform; stock v1 preserved inside). |

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
[FD-0001](founding/0001-motion-intentions/00-Vision.md).
