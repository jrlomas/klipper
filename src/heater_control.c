// Host-configured, MCU-executed heater control.
//
// The host owns policy, calibration, and target selection.  Once configured,
// this module owns the ADC-to-PWM real-time loop and continues through loss of
// host traffic.  Sensor validity, sample deadline, hard ceiling, and bounded
// autonomous duration remain independent of the PID arithmetic.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "adc_stream.h"
#include "autoconf.h" // CONFIG_WANT_TRAJECTORY
#include "basecmd.h" // oid_alloc
#include "board/gpio.h" // gpio_out_*
#include "board/irq.h" // irq_save
#include "board/misc.h" // timer_read_time
#include "command.h" // DECL_COMMAND
#if CONFIG_WANT_TRAJECTORY
#include "execlog.h" // execlog_append
#else
#define EL_HEATER 6
#define EL_FAULT 7
#endif
#include "generic/heater_control_math.h"
#include "gpiocmds.h" // digital_out_takeover_pin
#include "sched.h" // timers, tasks, and shutdown hooks

enum heater_control_state {
    HC_DISABLED,
    HC_READY,
    HC_ACTIVE,
    HC_AUTONOMOUS,
    HC_MANUAL,
    HC_FAULT,
};

enum heater_control_fault {
    HC_FAULT_SENSOR_RANGE = 1u << 0,
    HC_FAULT_SAMPLE_TIMEOUT = 1u << 1,
    HC_FAULT_MAX_TEMP = 1u << 2,
    HC_FAULT_AUTONOMOUS_TIMEOUT = 1u << 3,
    HC_FAULT_HEATING_RATE = 1u << 4,
};

enum heater_control_algorithm {
    HC_ALGO_PID,
    HC_ALGO_PREDICTIVE,
};

struct heater_control {
    struct timer pwm_timer;
    struct timer sample_timer;
    struct gpio_out heater_out;
    struct heater_pid_config pid_config;
    struct heater_pid_state pid_state;
    struct heater_predictive_config predictive_config;
    struct heater_predictive_state predictive_state;
    struct heater_verify_config verify_config;
    struct heater_verify_state verify_state;
    uint32_t heater_pin;
    uint32_t cycle_ticks;
    uint32_t phase_on_ticks;
    uint32_t sample_deadline_ticks;
    uint32_t host_timeout_ticks;
    uint32_t loop_period_ticks;
    uint32_t autonomous_max_samples;
    uint32_t autonomous_samples;
    uint32_t last_host_clock;
    uint32_t last_sample_clock;
    uint32_t last_run_clock;
    uint32_t previous_run_clock;
    uint32_t loop_dt_count;
    int64_t loop_jitter_sum_us;
    uint64_t loop_jitter_sumsq_us;
    int32_t loop_jitter_min_us;
    int32_t loop_jitter_max_us;
    uint32_t sample_count;
    uint32_t min_valid_adc, max_valid_adc, max_temp_adc;
    uint32_t last_adc;
    uint32_t target_adc;
    uint32_t manual_guard_adc;
    uint32_t manual_ceiling_adc;
    int32_t target_mdeg;
    int32_t slope_q16;
    int32_t manual_guard_mdeg;
    int32_t manual_guard_slope_q16;
    int32_t last_temp_mdeg;
    int32_t ambient_mdeg;
    uint16_t output_q16;
    uint16_t manual_output_q16;
    uint8_t oid;
    uint8_t invert_output;
    uint8_t invert_sense;
    uint8_t state;
    uint8_t fault;
    uint8_t pwm_phase;
    uint8_t pwm_running;
    uint8_t algorithm;
    uint8_t deadline_armed;
    uint8_t fault_event_pending;
};

static struct task_wake heater_control_fault_wake;

static void
heater_control_log(uint8_t type, struct heater_control *h, uint32_t clock,
                   uint32_t arg0, uint32_t arg1)
{
#if CONFIG_WANT_TRAJECTORY
    execlog_append(type, h->oid, clock, arg0, arg1);
#else
    (void)type;
    (void)h;
    (void)clock;
    (void)arg0;
    (void)arg1;
#endif
}

static void
heater_control_write(struct heater_control *h, uint8_t on)
{
    gpio_out_write(h->heater_out, !!on ^ h->invert_output);
}

static uint_fast8_t
heater_control_pwm_event(struct timer *timer)
{
    struct heater_control *h = container_of(
        timer, struct heater_control, pwm_timer);
    uint32_t cycle = h->cycle_ticks;
    if (!h->output_q16 || h->state == HC_FAULT
        || h->state == HC_DISABLED) {
        heater_control_write(h, 0);
        h->pwm_running = h->pwm_phase = 0;
        return SF_DONE;
    }
    if (!h->pwm_phase) {
        uint64_t scaled = (uint64_t)cycle * h->output_q16;
        uint32_t on_ticks = (scaled + HEATER_CONTROL_OUTPUT_ONE / 2)
                            / HEATER_CONTROL_OUTPUT_ONE;
        h->phase_on_ticks = on_ticks;
        heater_control_write(h, 1);
        if (on_ticks >= cycle) {
            h->pwm_timer.waketime += cycle;
            return SF_RESCHEDULE;
        }
        if (!on_ticks) {
            heater_control_write(h, 0);
            h->pwm_timer.waketime += cycle;
            return SF_RESCHEDULE;
        }
        h->pwm_phase = 1;
        h->pwm_timer.waketime += on_ticks;
        return SF_RESCHEDULE;
    }
    heater_control_write(h, 0);
    h->pwm_phase = 0;
    h->pwm_timer.waketime += h->cycle_ticks - h->phase_on_ticks;
    return SF_RESCHEDULE;
}

static void
heater_control_set_output(struct heater_control *h, uint16_t output)
{
    if (output > h->pid_config.max_output)
        output = h->pid_config.max_output;
    irqstatus_t flag = irq_save();
    h->output_q16 = output;
    if (!output) {
        sched_del_timer(&h->pwm_timer);
        h->pwm_running = h->pwm_phase = 0;
        heater_control_write(h, 0);
    } else if (!h->pwm_running) {
        h->pwm_running = 1;
        h->pwm_phase = 0;
        h->pwm_timer.waketime = timer_read_time() + timer_from_us(50);
        sched_add_timer(&h->pwm_timer);
    }
    irq_restore(flag);
}

static void
heater_control_disarm_deadline(struct heater_control *h)
{
    if (!h->deadline_armed)
        return;
    sched_del_timer(&h->sample_timer);
    h->deadline_armed = 0;
}

static void
heater_control_arm_deadline(struct heater_control *h, uint32_t clock)
{
    heater_control_disarm_deadline(h);
    h->sample_timer.waketime = clock + h->sample_deadline_ticks;
    h->deadline_armed = 1;
    sched_add_timer(&h->sample_timer);
}

static void
heater_control_fault(struct heater_control *h, uint8_t reason)
{
    h->fault |= reason;
    h->state = HC_FAULT;
    heater_control_disarm_deadline(h);
    heater_control_set_output(h, 0);
    heater_control_log(EL_FAULT, h, timer_read_time(), reason, h->last_adc);
    h->fault_event_pending = 1;
    sched_wake_task(&heater_control_fault_wake);
}

static uint_fast8_t
heater_control_sample_timeout(struct timer *timer)
{
    struct heater_control *h = container_of(
        timer, struct heater_control, sample_timer);
    h->deadline_armed = 0;
    heater_control_fault(h, HC_FAULT_SAMPLE_TIMEOUT);
    return SF_DONE;
}

static uint8_t
heater_control_too_hot(struct heater_control *h, uint32_t adc)
{
    uint32_t ceiling = (h->state == HC_MANUAL && h->manual_ceiling_adc
                        ? h->manual_ceiling_adc : h->max_temp_adc);
    return h->invert_sense ? adc >= ceiling : adc <= ceiling;
}

static void
heater_control_adc_update(void *context, uint32_t adc, uint32_t clock)
{
    struct heater_control *h = context;
    h->last_run_clock = timer_read_time();
    if (h->previous_run_clock) {
        uint32_t elapsed = h->last_run_clock - h->previous_run_clock;
        int32_t jitter_ticks = (int32_t)(elapsed - h->loop_period_ticks);
        uint32_t ticks_per_us = timer_from_us(1);
        int32_t jitter_us = ticks_per_us ? jitter_ticks / (int32_t)ticks_per_us
                                        : jitter_ticks;
        if (!h->loop_dt_count) {
            h->loop_jitter_min_us = h->loop_jitter_max_us = jitter_us;
        } else {
            if (jitter_us < h->loop_jitter_min_us)
                h->loop_jitter_min_us = jitter_us;
            if (jitter_us > h->loop_jitter_max_us)
                h->loop_jitter_max_us = jitter_us;
        }
        h->loop_dt_count++;
        h->loop_jitter_sum_us += jitter_us;
        h->loop_jitter_sumsq_us += (int64_t)jitter_us * jitter_us;
    }
    h->previous_run_clock = h->last_run_clock;
    h->last_sample_clock = clock;
    h->last_adc = adc;
    h->sample_count++;

    if (adc < h->min_valid_adc || adc > h->max_valid_adc) {
        heater_control_fault(h, HC_FAULT_SENSOR_RANGE);
        return;
    }
    if (heater_control_too_hot(h, adc)) {
        heater_control_fault(h, HC_FAULT_MAX_TEMP);
        return;
    }
    if (h->state == HC_FAULT || h->state == HC_DISABLED)
        return;
    if (h->state == HC_ACTIVE || h->state == HC_AUTONOMOUS
        || h->state == HC_MANUAL)
        heater_control_arm_deadline(h, h->last_run_clock);
    else
        heater_control_disarm_deadline(h);

    uint32_t control_adc = h->state == HC_MANUAL
                           ? h->manual_guard_adc : h->target_adc;
    int32_t control_mdeg = h->state == HC_MANUAL
                           ? h->manual_guard_mdeg : h->target_mdeg;
    int32_t control_slope = h->state == HC_MANUAL
                            ? h->manual_guard_slope_q16 : h->slope_q16;
    int32_t error_mdeg = control_adc ? heater_control_adc_error_mdeg(
        adc, control_adc, control_slope) : 0;
    if (control_adc)
        h->last_temp_mdeg = control_mdeg - error_mdeg;
    if (heater_verify_update(&h->verify_state, &h->verify_config,
                             h->last_temp_mdeg, !!control_adc,
                             control_mdeg)) {
        heater_control_fault(h, HC_FAULT_HEATING_RATE);
        return;
    }
    uint32_t silence = h->last_run_clock - h->last_host_clock;
    if (h->state == HC_ACTIVE && silence > h->host_timeout_ticks) {
        h->state = HC_AUTONOMOUS;
        h->autonomous_samples = 0;
        heater_control_log(EL_HEATER, h, clock, h->last_adc, h->state);
    }
    if (h->state == HC_MANUAL && silence > h->host_timeout_ticks) {
        h->manual_output_q16 = 0;
        h->manual_guard_adc = 0;
        h->manual_ceiling_adc = 0;
        h->state = HC_READY;
        heater_control_disarm_deadline(h);
        heater_control_set_output(h, 0);
        heater_control_log(EL_HEATER, h, clock, h->last_adc, h->state);
        return;
    }
    if (h->state == HC_AUTONOMOUS && h->autonomous_max_samples
        && ++h->autonomous_samples >= h->autonomous_max_samples) {
        heater_control_fault(h, HC_FAULT_AUTONOMOUS_TIMEOUT);
        return;
    }

    if (h->state == HC_MANUAL)
        heater_control_set_output(h, h->manual_output_q16);
    else if (!h->target_adc)
        heater_control_set_output(h, 0);
    else if (h->algorithm == HC_ALGO_PREDICTIVE)
        heater_control_set_output(h, heater_predictive_update(
            &h->predictive_state, &h->predictive_config,
            h->last_temp_mdeg, h->target_mdeg, h->ambient_mdeg,
            error_mdeg));
    else
        heater_control_set_output(h, heater_pid_update(
            &h->pid_state, &h->pid_config, h->last_temp_mdeg, error_mdeg));
}

void
command_config_heater_control(uint32_t *args)
{
    struct heater_control *h = oid_alloc(
        args[0], command_config_heater_control, sizeof(*h));
    h->oid = args[0];
    h->heater_pin = args[1];
    h->invert_output = args[2];
    h->cycle_ticks = args[3];
    h->state = HC_READY;
    h->pwm_timer.func = heater_control_pwm_event;
    h->sample_timer.func = heater_control_sample_timeout;
    uint8_t off = h->invert_output;
    if (digital_out_takeover_pin(h->heater_pin, off))
        shutdown("Autonomous heater could not claim PWM pin");
    h->heater_out = gpio_out_setup(h->heater_pin, off);
    heater_control_write(h, 0);
}
DECL_COMMAND(command_config_heater_control,
             "config_heater_control oid=%c heater_pin=%u invert=%c"
             " cycle_ticks=%u");

void
command_heater_control_setup(uint32_t *args)
{
    struct heater_control *h = oid_lookup(
        args[0], command_config_heater_control);
    h->min_valid_adc = args[1];
    h->max_valid_adc = args[2];
    h->max_temp_adc = args[3];
    h->invert_sense = args[4];
    h->sample_deadline_ticks = args[5];
    h->host_timeout_ticks = args[6];
    h->loop_period_ticks = args[7];
    h->autonomous_max_samples = args[8];
    h->pid_config.max_output = args[9];
    h->pid_config.kp_q20 = args[10];
    h->pid_config.ki_step_q20 = args[11];
    h->pid_config.kd_step_q20 = args[12];
    h->pid_config.derivative_alpha_q15 = args[13];
    h->last_host_clock = timer_read_time();
    heater_pid_reset(&h->pid_state);
    heater_predictive_reset(&h->predictive_state);
}
DECL_COMMAND(command_heater_control_setup,
             "heater_control_setup oid=%c min_adc=%u max_adc=%u"
             " max_temp_adc=%u invert_sense=%c sample_deadline=%u"
             " host_timeout=%u loop_period=%u autonomous_max_samples=%u"
             " max_output=%hu"
             " kp_q20=%i ki_step_q20=%i kd_step_q20=%i d_alpha_q15=%hu");

void
command_heater_control_set_predictive(uint32_t *args)
{
    struct heater_control *h = oid_lookup(
        args[0], command_config_heater_control);
    uint32_t retention = args[1], observer_alpha = args[2];
    uint32_t response_mdeg = args[3], effort_mdeg = args[4];
    if (retention > HEATER_CONTROL_ALPHA_ONE
        || !observer_alpha || observer_alpha > HEATER_CONTROL_ALPHA_ONE
        || !response_mdeg || response_mdeg > 1000000
        || effort_mdeg > 1000000 || args[5] > 1000000)
        shutdown("Invalid predictive heater model");
    h->predictive_config.retention_q15 = retention;
    h->predictive_config.observer_alpha_q15 = observer_alpha;
    h->predictive_config.max_output = h->pid_config.max_output;
    h->predictive_config.response_mdeg = response_mdeg;
    h->predictive_config.effort_mdeg = effort_mdeg;
    h->predictive_config.control_band_mdeg = args[5];
    h->predictive_config.integral_step_q20 = args[6];
    h->predictive_config.max_output_step = args[7];
    h->ambient_mdeg = args[8];
    h->algorithm = HC_ALGO_PREDICTIVE;
    heater_predictive_reconfigure(&h->predictive_state);
}
DECL_COMMAND(command_heater_control_set_predictive,
             "heater_control_set_predictive oid=%c retention_q15=%hu"
             " observer_alpha_q15=%hu response_mdeg=%u effort_mdeg=%u"
             " control_band_mdeg=%u integral_step_q20=%i max_step=%hu"
             " ambient_mdeg=%i");

void
command_heater_control_set_verify(uint32_t *args)
{
    struct heater_control *h = oid_lookup(
        args[0], command_config_heater_control);
    h->verify_config.period_ms = args[1];
    h->verify_config.hysteresis_mdeg = args[2];
    h->verify_config.max_error_mdeg_ms = args[3];
    h->verify_config.heating_gain_mdeg = args[4];
    h->verify_config.gain_samples = args[5];
    if (!h->verify_config.period_ms || !h->verify_config.max_error_mdeg_ms
        || !h->verify_config.heating_gain_mdeg
        || !h->verify_config.gain_samples)
        shutdown("Invalid autonomous heater verification policy");
}
DECL_COMMAND(command_heater_control_set_verify,
             "heater_control_set_verify oid=%c period_ms=%u"
             " hysteresis_mdeg=%u max_error_mdeg_ms=%u"
             " heating_gain_mdeg=%u gain_samples=%u");

void
command_heater_control_bind(uint32_t *args)
{
    struct heater_control *h = oid_lookup(
        args[0], command_config_heater_control);
    if (adc_stream_bind_local(args[1], args[2],
                              heater_control_adc_update, h))
        shutdown("Unable to bind autonomous heater ADC stream");
}
DECL_COMMAND(command_heater_control_bind,
             "heater_control_bind oid=%c stream_oid=%c sub=%c");

void
command_heater_control_set_target(uint32_t *args)
{
    struct heater_control *h = oid_lookup(
        args[0], command_config_heater_control);
    uint32_t target_adc = args[1];
    if (!target_adc) {
        h->target_adc = 0;
        h->state = h->fault ? HC_FAULT : HC_READY;
        heater_control_disarm_deadline(h);
        heater_pid_reset(&h->pid_state);
        heater_predictive_reset(&h->predictive_state);
        heater_verify_reset(&h->verify_state, 0);
        heater_control_set_output(h, 0);
        return;
    }
    if (h->fault)
        return;
    h->target_adc = target_adc;
    h->target_mdeg = args[2];
    h->slope_q16 = args[3];
    h->last_host_clock = timer_read_time();
    h->state = HC_ACTIVE;
    heater_control_arm_deadline(h, h->last_host_clock);
    heater_verify_reset(&h->verify_state, 1);
}
DECL_COMMAND(command_heater_control_set_target,
             "heater_control_set_target oid=%c target_adc=%u"
             " target_mdeg=%i slope_q16=%i");

void
command_heater_control_set_profile(uint32_t *args)
{
    struct heater_control *h = oid_lookup(
        args[0], command_config_heater_control);
    h->pid_config.kp_q20 = args[1];
    h->pid_config.ki_step_q20 = args[2];
    h->pid_config.kd_step_q20 = args[3];
    h->pid_config.derivative_alpha_q15 = args[4];
    int32_t error_mdeg = h->target_adc ? heater_control_adc_error_mdeg(
        h->last_adc, h->target_adc, h->slope_q16) : 0;
    heater_pid_reconfigure(&h->pid_state, &h->pid_config, error_mdeg);
}
DECL_COMMAND(command_heater_control_set_profile,
             "heater_control_set_profile oid=%c kp_q20=%i"
             " ki_step_q20=%i kd_step_q20=%i d_alpha_q15=%hu");

void
command_heater_control_set_manual_guard(uint32_t *args)
{
    struct heater_control *h = oid_lookup(
        args[0], command_config_heater_control);
    h->manual_guard_adc = args[1];
    h->manual_guard_mdeg = args[2];
    h->manual_guard_slope_q16 = args[3];
    h->manual_ceiling_adc = args[4];
    heater_verify_reset(&h->verify_state, !!h->manual_guard_adc);
}
DECL_COMMAND(command_heater_control_set_manual_guard,
             "heater_control_set_manual_guard oid=%c guard_adc=%u"
             " guard_mdeg=%i slope_q16=%i ceiling_adc=%u");

void
command_heater_control_set_manual(uint32_t *args)
{
    struct heater_control *h = oid_lookup(
        args[0], command_config_heater_control);
    h->last_host_clock = timer_read_time();
    if (h->fault)
        return;
    h->manual_output_q16 = args[1];
    h->state = HC_MANUAL;
    heater_control_arm_deadline(h, h->last_host_clock);
    if (h->sample_count)
        heater_control_set_output(h, h->manual_output_q16);
}
DECL_COMMAND(command_heater_control_set_manual,
             "heater_control_set_manual oid=%c output=%hu");

void
command_heater_control_ping(uint32_t *args)
{
    struct heater_control *h = oid_lookup(
        args[0], command_config_heater_control);
    h->last_host_clock = timer_read_time();
    if (h->state == HC_AUTONOMOUS) {
        h->state = h->target_adc ? HC_ACTIVE : HC_READY;
        heater_control_log(EL_HEATER, h, timer_read_time(),
                           h->last_adc, h->state);
    }
}
DECL_COMMAND(command_heater_control_ping, "heater_control_ping oid=%c");

void
command_heater_control_clear_fault(uint32_t *args)
{
    struct heater_control *h = oid_lookup(
        args[0], command_config_heater_control);
    if (h->target_adc || !h->sample_count)
        return;
    h->fault = 0;
    h->state = HC_READY;
    h->last_host_clock = timer_read_time();
    heater_pid_reset(&h->pid_state);
    heater_predictive_reset(&h->predictive_state);
}
DECL_COMMAND(command_heater_control_clear_fault,
             "heater_control_clear_fault oid=%c");

static void
heater_control_send_state(struct heater_control *h)
{
    sendf("heater_control_state oid=%c state=%c fault=%c adc=%u"
          " target_adc=%u temp_mdeg=%i output=%hu samples=%u"
          " last_sample=%u last_run=%u",
          h->oid, h->state, h->fault, h->last_adc,
          h->target_adc, h->last_temp_mdeg, h->output_q16,
          h->sample_count, h->last_sample_clock, h->last_run_clock);
}

void
command_heater_control_query(uint32_t *args)
{
    struct heater_control *h = oid_lookup(
        args[0], command_config_heater_control);
    heater_control_send_state(h);
}
DECL_COMMAND_FLAGS(command_heater_control_query, HF_IN_SHUTDOWN,
                   "heater_control_query oid=%c");

void
command_heater_control_query_timing(uint32_t *args)
{
    struct heater_control *h = oid_lookup(
        args[0], command_config_heater_control);
    uint64_t sum = h->loop_jitter_sum_us;
    sendf("heater_control_timing oid=%c count=%u min_us=%i max_us=%i"
          " sum_lo=%u sum_hi=%i sumsq_lo=%u sumsq_hi=%u period_ticks=%u",
          h->oid, h->loop_dt_count, h->loop_jitter_min_us,
          h->loop_jitter_max_us, (uint32_t)sum, (int32_t)(sum >> 32),
          (uint32_t)h->loop_jitter_sumsq_us,
          (uint32_t)(h->loop_jitter_sumsq_us >> 32),
          h->loop_period_ticks);
}
DECL_COMMAND_FLAGS(command_heater_control_query_timing, HF_IN_SHUTDOWN,
                   "heater_control_query_timing oid=%c");

void
heater_control_fault_task(void)
{
    if (!sched_check_wake(&heater_control_fault_wake))
        return;
    uint8_t oid;
    struct heater_control *h;
    foreach_oid(oid, h, command_config_heater_control) {
        if (!h->fault_event_pending)
            continue;
        h->fault_event_pending = 0;
        sendf("heater_control_fault_event oid=%c state=%c fault=%c adc=%u"
              " target_adc=%u temp_mdeg=%i output=%hu samples=%u"
              " last_sample=%u last_run=%u",
              h->oid, h->state, h->fault, h->last_adc,
              h->target_adc, h->last_temp_mdeg, h->output_q16,
              h->sample_count, h->last_sample_clock, h->last_run_clock);
    }
}
DECL_TASK(heater_control_fault_task);

void
heater_control_shutdown(void)
{
    uint8_t oid;
    struct heater_control *h;
    foreach_oid(oid, h, command_config_heater_control) {
        sched_del_timer(&h->pwm_timer);
        sched_del_timer(&h->sample_timer);
        h->pwm_running = h->deadline_armed = 0;
        h->state = HC_DISABLED;
        h->output_q16 = 0;
        heater_control_write(h, 0);
    }
}
DECL_SHUTDOWN(heater_control_shutdown);

DECL_CONSTANT("HEATER_CONTROL_V1", 1);
DECL_CONSTANT("HEATER_CONTROL_V2", 1);
DECL_CONSTANT("HEATER_CONTROL_V3", 1);
