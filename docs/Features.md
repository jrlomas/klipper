# Features

HELIX inherits Klipper's entire feature set — the high-precision motion,
the kinematics, the tuning tools, the ecosystem — and adds a new
architectural layer beneath it. This page lists both: first what HELIX
adds, then the inherited Klipper capabilities it is built on. For the
*why* behind the new layer, read the [HELIX overview](HELIX.md); for the
rigorous version, the [FD-0001 canon](founding/0001-motion-intentions/00-Vision.md).

Every HELIX capability is **opt-in**. A configuration that doesn't ask
for them behaves exactly like the Klipper it grew from.

## What HELIX adds

* **Motion intentions — and the end of stepper-only.** The host sends
  short per-joint polynomial *segments* — where a joint should be and how
  it's moving — and the micro-controller owns a deep queue of them,
  integrates them against its own clock, and synthesizes the output. The
  deeper point is that a segment describes *motion*, not *step pulses*:
  the actuator becomes a swappable backend behind one protocol. Classic
  step/dir steppers and sampled **PWM/DAC** actuators are supported
  today, and the architecture is built so a **closed-loop BLDC/FOC**
  servo joint is just another backend on the same queue tomorrow — a
  door that the pre-computed step stream held permanently shut. *(FD-0001 doc [02](founding/0001-motion-intentions/02-Intention_Protocol.md),
  [04](founding/0001-motion-intentions/04-Actuator_Backends.md).)*

* **Higher-order motion.** Segments run up to **cubic and quintic
  (jerk- and snap-limited) Bézier** curves, chained with drift-free
  fixed-point integration proven bit-exact across the host and the MCU,
  so thousands of segments accumulate zero positional drift.

* **Pause-and-hold failure recovery.** A recoverable failure — a lost
  link, a loose connector, a rebooted board — no longer means
  `shutdown()`. The affected board finishes or ramps out its motion,
  holds position with the motors energized, keeps the heaters on a
  per-heater **failsafe policy** (the bed stays hot, the part stays
  stuck), and keeps a rolling **execution log**. On resume the host
  reconciles the exact stopping point from that log. *(FD-0001 doc [08](founding/0001-motion-intentions/08-Failure_Recovery.md).)*

* **Machine time.** Every board disciplines its clock to a shared
  machine time via a beacon and a control loop, so "do this at T" means
  the same instant across a mainboard, a CAN toolhead, and a WiFi
  accessory. *(FD-0001 doc [01](founding/0001-motion-intentions/01-Time_Model.md).)*

* **Networks as first-class transports.** The same protocol runs over
  UDP (Ethernet/WiFi), CAN, USB, and UART, because deep intention queues
  absorb link jitter. It is authenticated by default (truncated HMAC over
  a static PSK), with an optional **DTLS-class secure session** (rotating
  keys, per-board identity), a negotiable **forward-error-correction**
  framing trailer for lossy links, and **Ed25519-signed firmware images**
  the bootloader verifies before running. *(FD-0001 doc [07](founding/0001-motion-intentions/07-Link_Transport.md),
  [11](founding/0001-motion-intentions/11-Bootloader.md).)*

* **Hardware events instead of polling — a capability unlock.** Endstop
  and probe detection moves off a polled software timer onto on-chip
  **edge interrupts, analog comparators, and ADC watchdogs**. The
  microsecond stop latency is only the surface. Event-driven detection
  paired with DMA makes a whole class of things *possible that polling
  made impossible* in a real-time motion loop: catching an **overrun or
  fault the instant it occurs** rather than at the next sample,
  **DMA-driven ADC oversampling**, comparator-based analog triggers, and
  hardware input-capture timestamps. Homing and probing use this today
  (with automatic fall back to polling where the silicon can't); the
  broader capabilities it opens are now architecturally within reach.
  *(FD-0001 doc [09](founding/0001-motion-intentions/09-Hardware_Triggers.md).)*

* **One protocol library, declared not generated.** The wire protocol is
  implemented once as a freestanding C++ library (`lib/intentproto`).
  Commands are declared with an annotation macro beside the handler and
  register themselves before `main()` — no code generator, no build step
  that parses source, and the data dictionary is a serialization of the
  live registry, served not scraped. *(FD-0001 doc [10](founding/0001-motion-intentions/10-Protocol_Library.md).)*

* **One firmware across families.** STM32 and ESP32 speak the same
  protocol and expose the same **versioned board syscall table**, so a
  module is written once against the API, not once per chip. *(FD-0001 doc [13](founding/0001-motion-intentions/13-Syscall_API.md).)*

* **ESP32 as a network-native target.** A dual-core ESP32 runs bare-metal
  motion on one core with the radio stack quarantined on the other,
  making a WiFi toolhead a real, first-class target. *(FD-0001 doc [12](founding/0001-motion-intentions/12-ESP32_Architecture.md).)*

* **New console surface.** `HELIX_STATUS` reports exactly which
  capabilities each board's firmware was built with and which host
  subsystems are loaded; `TRAJECTORY_STATUS`, `FAILURE_RECOVERY_STATUS`,
  `RESUME_MOTION`, `RECONNECT_MCU`, `TIMESYNC_STATUS`, and more expose the
  new subsystems. See the [command reference](G-Codes.md) and the
  consolidated [HELIX command list](Helix_Commands.md).

## Inherited from Klipper

HELIX is built on Klipper and keeps all of its strengths:

* High precision stepper movement. An application processor calculates
  precise step times from the physics of acceleration and the machine
  kinematics (no Bresenham-style estimation), schedules each stepper
  event to 25 microseconds or better, and the micro-controller executes
  them at the requested time — quieter, more stable motion.

* Best in class performance. High stepping rates on both new and old
  micro-controllers — over 175K steps/s even on 8-bit parts, several
  million per second on recent ones — with timing that stays precise at
  speed. (See the [benchmarks](#step-benchmarks) below.)

* Multiple micro-controllers per printer, with host-side clock
  synchronization for drift between them — enabled with a few config
  lines, no special code.

* Configuration via a simple config file — no reflashing to change a
  setting.

* "Smooth Pressure Advance" to reduce extruder ooze and improve corners
  without instantaneous extruder speed changes.

* "Input Shaping" to reduce ringing/ghosting and enable faster printing
  at high quality.

* An "iterative solver" that computes step times from simple kinematic
  equations — easier porting to new robots, precise timing even with
  complex kinematics, no line segmentation.

* Hardware-agnostic timing, portable code (ARM, AVR, PRU and more),
  high-level Python for kinematics/G-code/thermal logic, custom
  programmable G-code macros, and a builtin JSON API server.

### Standard 3D-printer features (inherited)

* Works with Mainsail, Fluidd, OctoPrint, and other web interfaces.
* Standard slicer G-code support (SuperSlicer, Cura, PrusaSlicer, …).
* Multiple extruders, shared-heater and IDEX setups.
* Cartesian, delta, corexy, corexz, hybrid-corexy, hybrid-corexz,
  deltesian, rotary delta, polar, and cable-winch kinematics.
* Automatic bed leveling — tilt detection or full mesh, adaptive mesh,
  multi-Z leveling, most probes (including BL-Touch, servo, and eddy
  current), and axis-twist compensation.
* Automatic delta calibration (probe or manual).
* Run-time "exclude object" for multi-part prints.
* A broad range of temperature sensors (thermistors, AD595/597/849x,
  PT100/PT1000, MAX6675/31855/31856/31865, BME280, HTU21D, DS18B20,
  AHT1X/2X/3X, SHT3x, LM75, and custom), plus MCU/RPi internal sensors.
* Thermal heater protection enabled by default.
* Standard, nozzle, and temperature-controlled fans, tachometer
  monitoring, and math-formula fan control.
* Run-time TMC driver configuration (TMC2130, 2208/2224, 2209, 2240,
  2660, 5160) and current control for traditional drivers (AD5206,
  DAC084S085, MCP4451/4728/4018, PWM).
* Common LCD displays with a customizable default menu.
* Constant acceleration with look-ahead.
* Stepper-phase endstop for improved endstop accuracy.
* Filament presence, motion, and width sensors.
* Acceleration measurement (adxl345, mpu9250, mpu6050, lis2dw12,
  lis3dh, icm20948).
* Top-speed limiting for short zigzag moves.
* Sample configs for many common printers (see the
  [config directory](../config/)).

To get started, read the [installation](Installation.md) guide and the
[HELIX User Guide](Helix_User_Guide.md).

## Step Benchmarks

Stepper performance tests — total steps per second on the
micro-controller. HELIX's trajectory path changes *how* motion is
delivered, not the raw stepping ceiling below, which the classic path
still achieves.

| Micro-controller                | 1 stepper active  | 3 steppers active |
| ------------------------------- | ----------------- | ----------------- |
| 16Mhz AVR                       | 157K              | 99K               |
| 20Mhz AVR                       | 196K              | 123K              |
| SAMD21                          | 686K              | 471K              |
| STM32F042                       | 814K              | 578K              |
| Beaglebone PRU                  | 866K              | 708K              |
| STM32G0B1                       | 1103K             | 790K              |
| STM32F103                       | 1180K             | 818K              |
| SAM3X8E                         | 1273K             | 981K              |
| SAM4S8C                         | 1690K             | 1385K             |
| LPC1768                         | 1923K             | 1351K             |
| LPC1769                         | 2353K             | 1622K             |
| SAM4E8E                         | 2500K             | 1674K             |
| SAMD51                          | 3077K             | 1885K             |
| AR100                           | 3529K             | 2507K             |
| STM32G431                       | 3617K             | 2452K             |
| STM32F407                       | 3652K             | 2459K             |
| STM32F446                       | 3913K             | 2634K             |
| RP2040                          | 4000K             | 2571K             |
| RP2350                          | 4167K             | 2663K             |
| SAME70                          | 6667K             | 4737K             |
| STM32H723                       | 7429K             | 8619K             |

If unsure of the micro-controller on a particular board, find the
appropriate [config file](../config/) and look for the micro-controller
name in the comments at the top. Further details are in the
[Benchmarks document](Benchmarks.md).
