// Deterministic fixed-point heater PID arithmetic.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <limits.h>
#include "heater_control_math.h"

static int32_t
clamp_s32(int64_t value)
{
    if (value > INT32_MAX)
        return INT32_MAX;
    if (value < INT32_MIN)
        return INT32_MIN;
    return value;
}

int32_t
heater_control_adc_error_mdeg(uint32_t adc, uint32_t target_adc,
                              int32_t slope_q16)
{
    int64_t delta = (int64_t)target_adc - adc;
    int64_t scaled = delta * slope_q16;
    // Round symmetrically rather than biasing negative temperatures.
    if (scaled >= 0)
        scaled += 1 << 15;
    else
        scaled -= 1 << 15;
    return clamp_s32(scaled / (1 << 16));
}

void
heater_pid_reset(struct heater_pid_state *state)
{
    *state = (struct heater_pid_state) { };
}

static int64_t
gain_term(int32_t gain_q20, int32_t value_mdeg)
{
    return (int64_t)gain_q20 * value_mdeg / 1000;
}

void
heater_pid_reconfigure(struct heater_pid_state *state,
                       const struct heater_pid_config *config,
                       int32_t error_mdeg)
{
    if (!state->initialized)
        return;
    int64_t max_q20 = (int64_t)config->max_output << 4;
    int64_t integral = state->output_q20
                       - gain_term(config->kp_q20, error_mdeg);
    if (integral < 0)
        integral = 0;
    else if (integral > max_q20)
        integral = max_q20;
    state->integral_q20 = integral;
    state->derivative_mdeg = 0;
}

uint16_t
heater_pid_update(struct heater_pid_state *state,
                  const struct heater_pid_config *config,
                  int32_t temp_mdeg, int32_t error_mdeg)
{
    int32_t measured_delta = 0;
    if (!state->initialized) {
        state->previous_temp_mdeg = temp_mdeg;
        state->initialized = 1;
    } else {
        measured_delta = temp_mdeg - state->previous_temp_mdeg;
        state->previous_temp_mdeg = temp_mdeg;
    }

    uint32_t alpha = config->derivative_alpha_q15;
    if (!alpha || alpha > HEATER_CONTROL_ALPHA_ONE)
        alpha = HEATER_CONTROL_ALPHA_ONE;
    int64_t derivative_delta = (int64_t)measured_delta
                               - state->derivative_mdeg;
    int64_t derivative_step = derivative_delta * alpha;
    if (derivative_step >= 0)
        derivative_step += HEATER_CONTROL_ALPHA_ONE / 2;
    else
        derivative_step -= HEATER_CONTROL_ALPHA_ONE / 2;
    state->derivative_mdeg = clamp_s32(
        (int64_t)state->derivative_mdeg
        + derivative_step / HEATER_CONTROL_ALPHA_ONE);

    int64_t p_term = gain_term(config->kp_q20, error_mdeg);
    // Derivative on measurement: rising temperature reduces heater output.
    int64_t d_term = -gain_term(config->kd_step_q20,
                                state->derivative_mdeg);
    int64_t max_q20 = (int64_t)config->max_output << 4;
    int64_t i_candidate = state->integral_q20
                          + gain_term(config->ki_step_q20, error_mdeg);
    if (i_candidate < 0)
        i_candidate = 0;
    else if (i_candidate > max_q20)
        i_candidate = max_q20;

    int64_t candidate = p_term + i_candidate + d_term;
    // Conditional integration is an explicit anti-windup policy.  Integrate
    // inside the actuator range, or while the error would drive a saturated
    // controller back toward that range.
    if ((candidate >= 0 && candidate <= max_q20)
        || (candidate > max_q20 && error_mdeg < 0)
        || (candidate < 0 && error_mdeg > 0))
        state->integral_q20 = i_candidate;

    int64_t output = p_term + state->integral_q20 + d_term;
    if (output < 0)
        output = 0;
    else if (output > max_q20)
        output = max_q20;
    state->output_q20 = output;
    return (uint16_t)((output + 8) >> 4);
}

void
heater_verify_reset(struct heater_verify_state *state, uint8_t new_target)
{
    *state = (struct heater_verify_state) {
        .new_target = !!new_target,
    };
}

uint8_t
heater_verify_update(struct heater_verify_state *state,
                     const struct heater_verify_config *config,
                     int32_t temp_mdeg, uint8_t target_valid,
                     int32_t target_mdeg)
{
    if (!target_valid
        || temp_mdeg >= target_mdeg - (int32_t)config->hysteresis_mdeg) {
        heater_verify_reset(state, 0);
        return 0;
    }
    int32_t excess = target_mdeg - config->hysteresis_mdeg - temp_mdeg;
    state->error_mdeg_ms += (int64_t)excess * config->period_ms;
    if (!state->approaching) {
        if (!state->new_target)
            return state->error_mdeg_ms >= config->max_error_mdeg_ms;
        state->goal_mdeg = temp_mdeg + config->heating_gain_mdeg;
        state->remaining_samples = config->gain_samples;
        state->approaching = state->starting = 1;
        state->new_target = 0;
        return 0;
    }
    if (temp_mdeg >= state->goal_mdeg) {
        state->error_mdeg_ms = 0;
        state->goal_mdeg = temp_mdeg + config->heating_gain_mdeg;
        state->remaining_samples = config->gain_samples;
        state->starting = 0;
        return 0;
    }
    if (state->remaining_samples)
        state->remaining_samples--;
    if (!state->remaining_samples) {
        state->approaching = state->starting = 0;
    } else if (state->starting) {
        int32_t candidate = temp_mdeg + config->heating_gain_mdeg;
        if (candidate < state->goal_mdeg)
            state->goal_mdeg = candidate;
    }
    return 0;
}
