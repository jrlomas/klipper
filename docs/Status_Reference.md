# Status reference

> **This is Helix** — an evolution of Klipper. This page documents a
> capability Helix shares with upstream Klipper. New to Helix? Start with
> the **[Helix overview](HELIX.md)**.

This document is a reference of printer status information available
in Helix [macros](Command_Templates.md),
[display fields](Config_Reference.md#display), and via the
[API Server](API_Server.md).

The fields in this document are subject to change - if using an
attribute be sure to review the
[Config Changes document](Config_Changes.md) when upgrading the
Helix software.

## angle

The following information is available in
[angle some_name](Config_Reference.md#angle) objects:
- `temperature`: The last temperature reading (in Celsius) from a
  tle5012b magnetic hall sensor. This value is only available if the
  angle sensor is a tle5012b chip and if measurements are in progress
  (otherwise it reports `None`).

## bed_mesh

The following information is available in the
[bed_mesh](Config_Reference.md#bed_mesh) object:
- `profile_name`, `mesh_min`, `mesh_max`, `probed_matrix`,
  `mesh_matrix`: Information on the currently active bed_mesh.
- `profiles`: The set of currently defined profiles as setup
   using BED_MESH_PROFILE.

## bed_screws

The following information is available in the
[bed_screws](Config_Reference.md#bed_screws) object:
- `is_active`: Returns True if the bed screws adjustment tool is currently
active.
- `state`: The bed screws adjustment tool state. It is one of
the following strings: "adjust", "fine".
- `current_screw`: The index for the current screw being adjusted.
- `accepted_screws`: The number of accepted screws.

## canbus_stats

The following information is available in the `canbus_stats
some_mcu_name` object (this object is automatically available if an
mcu is configured to use canbus):
- `rx_error`: The number of receive errors detected by the
  micro-controller canbus hardware.
- `tx_error`: The number of transmit errors detected by the
  micro-controller canbus hardware.
- `tx_retries`: On rp2XXX, the controller retry count. On STM32 FDCAN HELIX
  builds, the number of pending hardware transmissions cancelled after their
  bounded useful-arrival deadline; the protocol ARQ may subsequently retry
  the command block.
- `bus_state`: The status of the interface (typically "active" for a
  bus in normal operation, "warn" for a bus with recent errors,
  "passive" for a bus that will no longer transmit canbus error
  frames, or "off" for a bus that will no longer transmit or receive
  messages).

The rp2XXX implementation always reports `tx_error` as zero and `bus_state`
as "active". STM32 FDCAN reports protocol errors, bus state, and stale-buffer
cancellations.

## helix_can

The following information is available in each
[`helix_can bus_name`](Config_Reference.md#helix_can) object:

- `interface`: Managed SocketCAN interface (normally `helixcan0`).
- `profile`, `nominal_bitrate`, and `data_bitrate`: The unanimously selected
  and read-back active profile.
- `required_nodes`: Canonical full board identities required on the bus.
- `epoch` and `state`: Profile transaction epoch and lifecycle state
  (`bootstrap`, `preparing`, `active`, `maintenance`, or `failed`).
- `time_epoch` and `time_source`: Two-step CAN time epoch and
  `usb_sof_can_timestamp` when a composite bridge owns the source.
- `bridge_can`: Cumulative controller error/retry counters plus bridge queue
  drops, depth, high-water mark, accepted hardware frames, forwarded USB
  frames, and unaccounted handoff. Compare deltas to isolate one print.

## configfile

The following information is available in the `configfile` object
(this object is always available):
- `settings.<section>.<option>`: Returns the given config file setting
  (or default value) during the last software start or restart. (Any
  settings changed at run-time will not be reflected here.)
- `config.<section>.<option>`: Returns the given raw config file
  setting as read by Helix during the last software start or
  restart. (Any settings changed at run-time will not be reflected
  here.) All values are returned as strings.
- `save_config_pending`: Returns true if there are updates that a
  `SAVE_CONFIG` command may persist to disk.
- `save_config_pending_items`: Contains the sections and options that
  were changed and would be persisted by a `SAVE_CONFIG`.
- `warnings`: A list of warnings about config options. Each entry in
  the list will be a dictionary containing a `type` and `message`
  field (both strings). Additional fields may be available depending
  on the type of warning.

## display_status

The following information is available in the `display_status` object
(this object is automatically available if a
[display](Config_Reference.md#display) config section is defined):
- `progress`: The progress value of the last `M73` G-Code command (or
  `virtual_sdcard.progress` if no recent `M73` received).
- `message`: The message contained in the last `M117` G-Code command.

## endstop_phase

The following information is available in the
[endstop_phase](Config_Reference.md#endstop_phase) object:
- `last_home.<stepper name>.phase`: The phase of the stepper motor at
  the end of the last home attempt.
- `last_home.<stepper name>.phases`: The total number of phases
  available on the stepper motor.
- `last_home.<stepper name>.mcu_position`: The position (as tracked by
  the micro-controller) of the stepper motor at the end of the last
  home attempt. The position is the total number of steps taken in a
  forward direction minus the total number of steps taken in the
  reverse direction since the micro-controller was last restarted.

## exclude_object

The following information is available in the
[exclude_object](Exclude_Object.md) object:

- `objects`:  An array of the known objects as provided by the
  `EXCLUDE_OBJECT_DEFINE` command.  This is the same information provided by
  the `EXCLUDE_OBJECT VERBOSE=1` command. The `center` and `polygon` fields will
  only be present if provided in the original `EXCLUDE_OBJECT_DEFINE`

  Here is a JSON sample:
```
[
  {
    "polygon": [
      [ 156.25, 146.2511675 ],
      [ 156.25, 153.7488325 ],
      [ 163.75, 153.7488325 ],
      [ 163.75, 146.2511675 ]
    ],
    "name": "CYLINDER_2_STL_ID_2_COPY_0",
    "center": [ 160, 150 ]
  },
  {
    "polygon": [
      [ 146.25, 146.2511675 ],
      [ 146.25, 153.7488325 ],
      [ 153.75, 153.7488325 ],
      [ 153.75, 146.2511675 ]
    ],
    "name": "CYLINDER_2_STL_ID_1_COPY_0",
    "center": [ 150, 150 ]
  }
]
```
- `excluded_objects`: An array of strings listing the names of excluded objects.
- `current_object`: The name of the object currently being printed.

## extruder_stepper

The following information is available for extruder_stepper objects (as well as
[extruder](Config_Reference.md#extruder) objects):
- `pressure_advance`: The current [pressure advance](Pressure_Advance.md) value.
- `smooth_time`: The current pressure advance smooth time.
- `motion_queue`: The name of the extruder that this extruder stepper is
  currently synchronized to.  This is reported as `None` if the extruder stepper
  is not currently associated with an extruder.

## fan

The following information is available in
[fan](Config_Reference.md#fan),
[heater_fan some_name](Config_Reference.md#heater_fan) and
[controller_fan some_name](Config_Reference.md#controller_fan)
objects:
- `speed`: The fan speed as a float between 0.0 and 1.0.
- `rpm`: The measured fan speed in rotations per minute if the fan has
  a tachometer_pin defined.

## filament_switch_sensor

The following information is available in
[filament_switch_sensor some_name](Config_Reference.md#filament_switch_sensor)
objects:
- `enabled`: Returns True if the switch sensor is currently enabled.
- `filament_detected`: Returns True if the sensor is in a triggered
  state.

## filament_motion_sensor

The following information is available in
[filament_motion_sensor some_name](Config_Reference.md#filament_motion_sensor)
objects:
- `enabled`: Returns True if the motion sensor is currently enabled.
- `filament_detected`: Returns True if the sensor is in a triggered
  state.

## firmware_retraction

The following information is available in the
[firmware_retraction](Config_Reference.md#firmware_retraction) object:
- `retract_length`, `retract_speed`, `unretract_extra_length`,
  `unretract_speed`: The current settings for the firmware_retraction
  module. These settings may differ from the config file if a
  `SET_RETRACTION` command alters them.

## gcode

The following information is available in the `gcode` object:
- `commands`: Returns a list of all currently available commands. For each
  command, if a help string is defined it will also be provided.

## gcode_button

The following information is available in
[gcode_button some_name](Config_Reference.md#gcode_button) objects:
- `state`: The current button state returned as "PRESSED" or "RELEASED"

## gcode_macro

The following information is available in
[gcode_macro some_name](Config_Reference.md#gcode_macro) objects:
- `<variable>`: The current value of a
  [gcode_macro variable](Command_Templates.md#variables).

## gcode_move

The following information is available in the `gcode_move` object
(this object is always available):
- `gcode_position`: The current position of the toolhead relative to
  the current G-Code origin. That is, positions that one might
  directly send to a `G1` command. This value is encoded as a
  [coordinate](#accessing-coordinates).
- `position`: The last commanded position of the toolhead using the
  coordinate system specified in the config file. This value is
  encoded as a [coordinate](#accessing-coordinates).
- `homing_origin`: The origin of the gcode coordinate system (relative
  to the coordinate system specified in the config file) to use after
  a `G28` command. The `SET_GCODE_OFFSET` command can alter this
  position. This value is encoded as a
  [coordinate](#accessing-coordinates).
- `speed`: The last speed set in a `G1` command (in mm/s).
- `speed_factor`: The "speed factor override" as set by an `M220`
  command. This is a floating point value such that 1.0 means no
  override and, for example, 2.0 would double requested speed.
- `extrude_factor`: The "extrude factor override" as set by an `M221`
  command. This is a floating point value such that 1.0 means no
  override and, for example, 2.0 would double requested extrusions.
- `absolute_coordinates`: This returns True if in `G90` absolute
  coordinate mode or False if in `G91` relative mode.
- `absolute_extrude`: This returns True if in `M82` absolute extrude
  mode or False if in `M83` relative mode.
- `axis_map`: Provides a mechanism for finding the coordinate
  component for a given G-Code id that is used in `G1` commands. See
  the [Accessing Coordinates](#accessing-coordinates) section for
  details.

## hall_filament_width_sensor

The following information is available in the
[hall_filament_width_sensor](Config_Reference.md#hall_filament_width_sensor)
object:
- all items from
  [filament_switch_sensor](Status_Reference.md#filament_switch_sensor)
- `is_active`: Returns True if the sensor is currently active.
- `flow_compensation_enabled`: Returns True if flow compensation is enabled.
- `Diameter`: Returns the last width reading in mm if the sensor is active or
  the nominal filament diameter if it is not.
- `Raw`: The last raw ADC reading from the sensor.

## heater

The following information is available for heater objects such as
[extruder](Config_Reference.md#extruder),
[heater_bed](Config_Reference.md#heater_bed), and
[heater_generic](Config_Reference.md#heater_generic):
- `temperature`: The last reported temperature (in Celsius as a float)
  for the given heater.
- `target`: The current target temperature (in Celsius as a float) for
  the given heater.
- `power`: The last setting of the pulse-width modulation (PWM) pin (a
  value between 0.0 and 1.0) associated with the heater.
- `can_extrude`: If extruder can extrude (defined by `min_extrude_temp`),
  available only for [extruder](Config_Reference.md#extruder)

## heaters

The following information is available in the `heaters` object (this
object is available if any heater is defined):
- `available_heaters`: Returns a list of all currently available
  heaters by their full config section names, e.g. `["extruder",
  "heater_bed", "heater_generic my_custom_heater"]`.
- `available_sensors`: Returns a list of all currently available
  temperature sensors by their full config section names,
  e.g. `["extruder", "heater_bed", "heater_generic my_custom_heater",
  "temperature_sensor electronics_temp"]`.
- `available_monitors`: Returns a list of all currently available
  temperature monitors by their full config section names,
  e.g. `["tmc2240 stepper_x"]`.  While a temperature sensor is always
  available to read, a temperature monitor may not be available and
  will return null in such case.

## idle_timeout

The following information is available in the
[idle_timeout](Config_Reference.md#idle_timeout) object (this object
is always available):
- `state`: The current state of the printer as tracked by the
  idle_timeout module. It is one of the following strings: "Idle",
  "Printing", "Ready".
- `printing_time`: The amount of time (in seconds) the printer has
  been in the "Printing" state (as tracked by the idle_timeout
  module).
- `idle_timeout`: The current 'timeout' (in seconds)
   to wait for the gcode to be triggered.
   (as set by [SET_IDLE_TIMEOUT](G-Codes.md#set_idle_timeout))

## led

The following information is available for each `[led led_name]`,
`[neopixel led_name]`, `[dotstar led_name]`, `[pca9533 led_name]`, and
`[pca9632 led_name]` config section defined in printer.cfg:
- `color_data`: A list of color lists containing the RGBW values for a
  led in the chain. Each value is represented as a float from 0.0 to
  1.0. Each color list contains 4 items (red, green, blue, white) even
  if the underlying LED supports fewer color channels. For example,
  the blue value (3rd item in color list) of the second neopixel in a
  chain could be accessed at
  `printer["neopixel <config_name>"].color_data[1][2]`.

## load_cell

The following information is available for each `[load_cell name]`:
- `is_calibrated`: True/False whether the load cell is calibrated.
- `counts_per_gram`: The number of raw sensor counts that equals 1 gram of force.
- `reference_tare_counts`: The reference number of raw sensor counts for 0 force.
- `tare_counts`: The current number of raw sensor counts for 0 force.
- `force_g`: The force in grams, averaged over the last polling period.
- `min_force_g`: The minimum force in grams, over the last polling period.
- `max_force_g`: The maximum force in grams, over the last polling period.
- `errors`: The number of sensor errors detected since the last start
  of measurements.
- `overflows`: The number of data buffer overflows detected since the last
  start of measurements.
- `sample_rate`: The sensor's sample rate in samples per second.

## load_cell_probe

The following information is available for `[load_cell_probe]`:
- all items from [load_cell](Status_Reference.md#load_cell)
- all items from [probe](Status_Reference.md#probe)
- `endstop_tare_counts`: The load cell probe keeps a tare value independent of
  the load cell. This is re-set at the start of each probe.
- `last_trigger_time`: Timestamp of the last homing trigger.
- `last_z_result`: The Z position result of the last tap.
- `is_last_tap_valid`: True if the last tap result is valid.

## manual_probe

The following information is available in the
`manual_probe` object:
- `is_active`: Returns True if a manual probing helper script is currently
active.
- `z_position`: The current height of the nozzle (as the printer currently
understands it).
- `z_position_lower`: Last probe attempt just lower than the current height.
- `z_position_upper`: Last probe attempt just greater than the current height.

## mcu

The following information is available in
[mcu](Config_Reference.md#mcu) and
[mcu some_name](Config_Reference.md#mcu-my_extra_mcu) objects:
- `mcu_version`: The Helix code version reported by the
  micro-controller.
- `mcu_build_versions`: Information on the build tools used to
  generate the micro-controller code (as reported by the
  micro-controller).
- `mcu_constants.<constant_name>`: Compile time constants reported by
  the micro-controller. The available constants may differ between
  micro-controller architectures and with each code revision.
- `last_stats.<statistics_name>`: Statistics information on the
  micro-controller connection.

## motion_report

The following information is available in the `motion_report` object
(this object is automatically available if any stepper config section
is defined):
- `live_position`: The requested toolhead position interpolated to the
  current time. This value is encoded as a
  [coordinate](#accessing-coordinates).
- `live_velocity`: The requested toolhead velocity (in mm/s) at the
  current time.
- `live_extruder_velocity`: The requested extruder velocity (in mm/s)
  at the current time.

## output_pin

The following information is available in
[output_pin some_name](Config_Reference.md#output_pin) and
[pwm_tool some_name](Config_Reference.md#pwm_tool) objects:
- `value`: The "value" of the pin, as set by a `SET_PIN` command.

## palette2

The following information is available in the
[palette2](Config_Reference.md#palette2) object:
- `ping`: Amount of the last reported Palette 2 ping in percent.
- `remaining_load_length`: When starting a Palette 2 print, this will
  be the amount of filament to load into the extruder.
- `is_splicing`: True when the Palette 2 is splicing filament.

## pause_resume

The following information is available in the
[pause_resume](Config_Reference.md#pause_resume) object:
- `is_paused`: Returns true if a PAUSE command has been executed
  without a corresponding RESUME.

## print_stats

The following information is available in the `print_stats` object
(this object is automatically available if a
[virtual_sdcard](Config_Reference.md#virtual_sdcard) config section is
defined):
- `filename`, `total_duration`, `print_duration`, `filament_used`,
  `state`, `message`: Estimated information about the current print when a
  virtual_sdcard print is active.
- `info.total_layer`: The total layer value of the last `SET_PRINT_STATS_INFO
   TOTAL_LAYER=<value>` G-Code command.
- `info.current_layer`: The current layer value of the last
  `SET_PRINT_STATS_INFO CURRENT_LAYER=<value>` G-Code command.

## probe

The following information is available in the
[probe](Config_Reference.md#probe) object (this object is also
available if a [bltouch](Config_Reference.md#bltouch) config section
is defined):
- `name`: Returns the name of the probe in use.
- `last_query`: Returns True if the probe was reported as "triggered"
  during the last QUERY_PROBE command. Note, if this is used in a
  macro, due to the order of template expansion, the QUERY_PROBE
  command must be run prior to the macro containing this reference.
- `last_probe_position`: The results of the last `PROBE` command. This
  value is encoded as a [coordinate](#accessing-coordinates). The
  probe hardware estimates that if one were to command the toolhead to
  XY position `last_probe_position.x`,`last_probe_position.y` and
  descend then the tip of the toolhead would first contact the bed at
  a Z height of `last_probe_position.z`. These coordinates are
  relative to the frame (that is, they use the coordinate system
  specified in the config file).  Note, if this is used in a macro,
  due to the order of template expansion, the `PROBE` command must be
  run prior to the macro containing this reference.
- `last_z_result`: This value is deprecated; it will be removed in the
  near future.

## pwm_cycle_time

The following information is available in
[pwm_cycle_time some_name](Config_Reference.md#pwm_cycle_time)
objects:
- `value`: The "value" of the pin, as set by a `SET_PIN` command.

## quad_gantry_level

The following information is available in the `quad_gantry_level` object
(this object is available if quad_gantry_level is defined):
- `applied`: True if the gantry leveling process has been run and completed
  successfully.

## query_endstops

The following information is available in the `query_endstops` object
(this object is available if any endstop is defined):
- `last_query["<endstop>"]`: Returns True if the given endstop was
  reported as "triggered" during the last QUERY_ENDSTOP command. Note,
  if this is used in a macro, due to the order of template expansion,
  the QUERY_ENDSTOP command must be run prior to the macro containing
  this reference.

## screws_tilt_adjust

The following information is available in the `screws_tilt_adjust`
object:
- `error`: Returns True if the most recent `SCREWS_TILT_CALCULATE`
  command included the `MAX_DEVIATION` parameter and any of the probed
  screw points exceeded the specified `MAX_DEVIATION`.
- `max_deviation`: Return the last `MAX_DEVIATION` value of the most
  recent `SCREWS_TILT_CALCULATE` command.
- `results["<screw>"]`: A dictionary containing the following keys:
  - `z`: The measured Z height of the screw location.
  - `sign`: A string specifying the direction to turn to screw for the
    necessary adjustment. Either "CW" for clockwise or "CCW" for
    counterclockwise.
  - `adjust`: The number of screw turns to adjust the screw, given in
    the format "HH:MM," where "HH" is the number of full screw turns
    and "MM" is the number of "minutes of a clock face" representing
    a partial screw turn. (E.g. "01:15" would mean to turn the screw
    one and a quarter revolutions.)
  - `is_base`: Returns True if this is the base screw.

## servo

The following information is available in
[servo some_name](Config_Reference.md#servo) objects:
- `printer["servo <config_name>"].value`: The last setting of the PWM
  pin (a value between 0.0 and 1.0) associated with the servo.

## skew_correction.py

The following information is available in the `skew_correction` object (this
object is available if any skew_correction is defined):
- `current_profile_name`: Returns the name of the currently loaded SKEW_PROFILE.

## stepper_enable

The following information is available in the `stepper_enable` object (this
object is available if any stepper is defined):
- `steppers["<stepper>"]`: Returns True if the given stepper is enabled.

## system_stats

The following information is available in the `system_stats` object
(this object is always available):
- `sysload`, `cputime`, `memavail`: Information on the host operating
  system and process load.

## temperature sensors

The following information is available in

[bme280 config_section_name](Config_Reference.md#bmp280bme280bme680-temperature-sensor),
[htu21d config_section_name](Config_Reference.md#htu21d-sensor),
[sht3x config_section_name](Config_Reference.md#sht31-sensor),
[lm75 config_section_name](Config_Reference.md#lm75-temperature-sensor),
[temperature_host config_section_name](Config_Reference.md#host-temperature-sensor)
and
[temperature_combined config_section_name](Config_Reference.md#combined-temperature-sensor)
objects:
- `temperature`: The last read temperature from the sensor.
- `humidity`, `pressure`, `gas`: The last read values from the sensor
  (only on bme280, htu21d, sht3x and lm75 sensors).

## temperature_fan

The following information is available in
[temperature_fan some_name](Config_Reference.md#temperature_fan)
objects:
- `temperature`: The last read temperature from the sensor.
- `target`: The target temperature for the fan.

## temperature_sensor

The following information is available in
[temperature_sensor some_name](Config_Reference.md#temperature_sensor)
objects:
- `temperature`: The last read temperature from the sensor.
- `measured_min_temp`, `measured_max_temp`: The lowest and highest
  temperature seen by the sensor since the Helix host software was
  last restarted.

## atlas_trace

The following information is available in the
[atlas_trace](Config_Reference.md#atlas_trace) object:
- `output`, `write_errors`, `ring_size`, and `stream_max`: The JSONL target,
  host write failures, per-MCU ring capacity, and configured send budget.
- `mcus.<mcu_name>.available` and `reason`: Whether that board advertises
  trace support, with an explanation for rolling-upgrade peers that do not.
- `records`, `last_seq`, and `last_machine_time`: Records accepted by the
  host and the latest sequence/time observed.
- `sequence_gaps`, `dropped`, and `unaccounted_gaps`: Total gaps observed by
  the host, ring overwrites explicitly counted by firmware, and any remaining
  transport/response-queue gaps not explained by those overwrites.
- `next_seq` and `oldest_seq`: The firmware ring's current half-open sequence
  bounds.

## timesync

The following information is available in the
[timesync](Config_Reference.md#timesync) object (FD-0001 doc 01):
- `machine_time`: The primary micro-controller's estimated print time
  (in seconds), exposed so external clients may synchronize to machine
  time. `None` until the beacon loop is active.
- `mcus.<mcu_name>.converged`: True once the named secondary micro-
  controller's discipline filter is within the configured `converge_window`
  and the host's two underlying clock models have remained rate-consistent
  with unchanged minimum-RTT anchors for the required stability window. This
  is an ingest/preflight state, not proof of absolute physical phase: link
  asymmetry is not observable in the MCU residual and must be bounded by the
  transport's timing class and external qualification.
- `mcus.<mcu_name>.last_err_ticks`: The last measured synchronization
  error for that micro-controller, in its own clock ticks.
- `mcus.<mcu_name>.rate`: The raw Q8.24 local-ticks-per-machine-tick rate
  applied by that micro-controller's discipline filter. `TIMESYNC_STATUS`
  reports its correction in ppm relative to the boards' nominal frequency
  ratio.
- `mcus.<mcu_name>.flags` and `prime_count`: The raw firmware discipline
  state and number of startup samples accumulated.
- `mcus.<mcu_name>.machine_ref` and `local_ref`: The current 32-bit mapping
  anchors published by the secondary micro-controller.
- `mcus.<mcu_name>.relay_machine_clock`, `relay_local_est`, and
  `relay_rate`: The latest host relay sample (using extended clocks) and the
  fitted local-ticks-per-machine-tick rate sent to the secondary.
- `mcus.<mcu_name>.raw_local_est` and `sample_rate`: The unfiltered latest
  cross-link estimate and ratio between the two most recent raw samples.
  Comparing these with the relay fields makes host-regression noise and
  MCU-filter faults distinguishable during bring-up.
- `primary_host_clock` and `mcus.<mcu_name>.host_clock`: Diagnostic host
  `ClockSync` frequency, minimum half-RTT anchor, prediction variance, and
  sample time used to bridge the two independent links.
- `mcus.<mcu_name>.host_model_stable`, `host_stable_count`, `host_rate`, and
  `host_rate_error_ppm`: The host-side machine-time preflight state. A minimum-RTT
  anchor change or disagreement between the direct clock-frequency ratio and
  robust relay fit clears this gate immediately; eight consecutive steady
  beacons are required to reacquire it. It rejects moving host models but
  cannot prove that two independent link midpoints are unbiased. In USB-SOF
  mode, the rate and error are instead derived from consistency between
  consecutive exact same-frame clock pairs; a startup software clock estimate
  cannot veto the hardware-derived rate.
- `mcus.<mcu_name>.usb_sof`: True when this secondary supports matched USB
  Start-of-Frame timestamps and exact-frame probing was attempted.
- `mcus.<mcu_name>.usb_sof_active` and `sof_pair_unavailable`: Whether
  exact-frame probing remains active, and whether it was disabled after eight
  consecutive unclassified misses. Separate USB root-port/frame domains may
  expose SOF counters that never match; that topology retains the qualified
  host clock regression instead of probing and warning indefinitely.
- `mcus.<mcu_name>.sof_capture_windows`, `sof_captured_frames`,
  `sof_discarded_frames`, `sof_discarded_primask_frames`,
  `sof_missed_frames`, `sof_guard_discard_matches`,
  `sof_guard_primask_matches`, and `sof_unclassified_misses`: Cumulative
  exact-frame observation and attribution counters. A positively attributed
  IRQ-guard discard may retain a bounded qualified holdover sample. An
  unclassified miss uses the host estimate so a non-pairable topology cannot
  freeze and expire an otherwise valid mapping.
- `mcus.<mcu_name>.sof_rate_bad_count`: Consecutive exact-pair observations
  outside the configured cross-MCU `converge_window`. Once lock is
  established, one or two observations are tolerated and three consecutive
  misses revoke the host gate. A bounded observation clears the count.
  Outlier magnitude alone does not revoke an established map because the
  exact-pair timestamp includes load-dependent ISR-entry latency.
- `mcus.<mcu_name>.sof_filtered_count`: Number of exact-pair observations
  rejected since connect because their interval rate was outside the qualified
  band. Once lock exists, firmware receives a one-beacon holdover prediction
  on the established SOF rate instead of the delayed ISR timestamp; sustained
  rejected observations still revoke the host gate.
- `mcus.<mcu_name>.sof_phase_error_us`: Latest raw exact-pair phase residual
  against the qualified SOF clock map, in microseconds. This is the quantity
  compared with `converge_window`; `host_rate_error_ppm` remains a diagnostic
  one-interval derivative and is not itself a Class-0 gate.

## trajectory_queuing

The following information is available in the
[trajectory_queuing](Config_Reference.md#trajectory_queuing) object
(FD-0001 doc 02), present whenever any stepper uses
`motion_protocol: trajectory`:
- `trajectory_steppers`: a list, one entry per trajectory actuator, each
  a dictionary with `name`, `oid`, `anchored`, `need_rebase`, `su_per_mm`,
  `commanded_pos_su`, `last_intention_pos_su`, `higher_order` (whether the
  firmware supports cubic/quintic segments), and `homing_volatile`.

## tmc drivers

The following information is available in
[TMC stepper driver](Config_Reference.md#tmc-stepper-driver-configuration)
objects (eg, `[tmc2208 stepper_x]`):
- `mcu_phase_offset`: The micro-controller stepper position
  corresponding with the driver's "zero" phase. This field may be null
  if the phase offset is not known.
- `phase_offset_position`: The "commanded position" corresponding to
  the driver's "zero" phase. This field may be null if the phase
  offset is not known.
- `drv_status`: The results of the last driver status query. (Only
  non-zero fields are reported.) This field will be null if the driver
  is not enabled (and thus is not periodically queried).
- `temperature`: The internal temperature reported by the driver. This
  field will be null if the driver is not enabled or if the driver
  does not support temperature reporting.
- `run_current`: The currently set run current.
- `hold_current`: The currently set hold current.

## toolhead

The following information is available in the `toolhead` object
(this object is always available):
- `position`: The last commanded position of the toolhead relative to
  the coordinate system specified in the config file. This value is
  encoded as a [coordinate](#accessing-coordinates).
- `extruder`: The name of the currently active extruder. For example,
  in a macro one could use `printer[printer.toolhead.extruder].target`
  to get the target temperature of the current extruder.
- `homed_axes`: The current cartesian axes considered to be in a
  "homed" state. This is a string containing one or more of "x", "y",
  "z".
- `axis_minimum`, `axis_maximum`: The axis travel limits (mm) after
  homing. This value is encoded as a
  [coordinate](#accessing-coordinates).
- For Delta printers the `cone_start_z` is the max z height at
  maximum radius (`printer.toolhead.cone_start_z`).
- `max_velocity`, `max_accel`, `minimum_cruise_ratio`,
  `square_corner_velocity`: The current printing limits that are in
  effect. This may differ from the config file settings if a
  `SET_VELOCITY_LIMIT` (or `M204`) command alters them at run-time.
- `stalls`: The total number of times (since the last restart) that
  the printer had to be paused because the toolhead moved faster than
  moves could be read from the G-Code input.
- `extra_axes`: Provides a mechanism for finding the coordinate
  component for extra axes available in standard `G1` type move
  commands. See the [Accessing Coordinates](#accessing-coordinates)
  section for details.

## dual_carriage

The following information is available in
[dual_carriage](Config_Reference.md#dual_carriage)
on a cartesian, hybrid_corexy or hybrid_corexz robot
- `carriage_0`: The mode of the carriage 0. Possible values are:
  "INACTIVE" and "PRIMARY".
- `carriage_1`: The mode of the carriage 1. Possible values are:
  "INACTIVE", "PRIMARY", "COPY", and "MIRROR".

On a `generic_cartesian` kinematic, the following information is
available in `dual_carriage`:
- `carriages["<carriage>"]`: The mode of the carriage `<carriage>`. Possible
  values are "INACTIVE" and "PRIMARY" for the primary carriage and "INACTIVE",
  "PRIMARY", "COPY", and "MIRROR" for the dual carriage.

## virtual_sdcard

The following information is available in the
[virtual_sdcard](Config_Reference.md#virtual_sdcard) object:
- `is_active`: Returns True if a print from file is currently active.
- `progress`: An estimate of the current print progress (based of file
  size and file position).
- `file_path`: A full path to the file of currently loaded file.
- `file_position`: The current position (in bytes) of an active print.
- `file_size`: The file size (in bytes) of currently loaded file.

## webhooks

The following information is available in the `webhooks` object (this
object is always available):
- `state`: Returns a string indicating the current Helix
  state. Possible values are: "ready", "startup", "shutdown", "error".
- `state_message`: A human readable string giving additional context
  on the current Helix state.

## z_thermal_adjust

The following information is available in the `z_thermal_adjust` object (this
object is available if [z_thermal_adjust](Config_Reference.md#z_thermal_adjust)
is defined).
- `enabled`: Returns True if adjustment is enabled.
- `temperature`: Current (smoothed) temperature of the defined sensor. [degC]
- `measured_min_temp`: Minimum measured temperature. [degC]
- `measured_max_temp`: Maximum measured temperature. [degC]
- `current_z_adjust`: Last computed Z adjustment [mm].
- `z_adjust_ref_temperature`: Current reference temperature used for calculation
  of Z `current_z_adjust` [degC].

## z_tilt

The following information is available in the `z_tilt` object (this
object is available if z_tilt is defined):
- `applied`: True if the z-tilt leveling process has been run and completed
  successfully.

## Accessing Coordinates

Some status fields provide a "coordinate". For macro users these
fields may be accessed by component name
(eg,`{printer.toolhead.position.x}`), where the component name may be
"x", "y", or "z".

For developers using the Helix API Server these fields are
transmitted as a list - for example: `{"toolhead": {"position": [1.0,
2.0, 3.0, 7.3, 19.2]}}` . The first three components of the list
correspond with the x, y, and z axes.

A coordinate will typically have at least 3 components (x, y, and z),
however there may also be additional components. Care should be taken
when accessing any of these additional components as the ordering and
number of components may change at run-time.

One may use `{printer.gcode_move.axis_map}` and/or
`{printer.toolhead.extra_axes}` to determine the number of components
and the ordering of components. For example, to access the "E"
component one could use
`{printer.toolhead.position[printer.gcode_move.axis_map.E]}`. Or, if
one wanted to find the component associated with the "extruder"
object, one could use
`{printer.toolhead.position[printer.toolhead.extra_axes.extruder]}`.
