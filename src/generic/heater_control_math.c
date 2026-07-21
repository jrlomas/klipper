// Deterministic fixed-point heater PID arithmetic.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <limits.h>
#include "heater_control_math.h"

#define HEATER_MODEL_TEMP_LIMIT_MDEG 2000000

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

void
heater_predictive_reset(struct heater_predictive_state *state)
{
    *state = (struct heater_predictive_state) { };
}

void
heater_predictive_reconfigure(struct heater_predictive_state *state)
{
    if (state->initialized)
        state->rebase_output = 1;
}

static int64_t
div_round_s64(int64_t numerator, uint64_t denominator)
{
    if (numerator >= 0)
        return (numerator + denominator / 2) / denominator;
    // Avoid negating INT64_MIN while forming the unsigned magnitude.
    uint64_t magnitude = (uint64_t)(-(numerator + 1)) + 1;
    return -(int64_t)((magnitude + denominator / 2) / denominator);
}

static int32_t
filter_mdeg(int32_t previous, int32_t sample, uint16_t alpha)
{
    if (!alpha || alpha > HEATER_CONTROL_ALPHA_ONE)
        alpha = HEATER_CONTROL_ALPHA_ONE;
    int64_t step = ((int64_t)sample - previous) * alpha;
    return clamp_s32((int64_t)previous + div_round_s64(
        step, HEATER_CONTROL_ALPHA_ONE));
}

uint16_t
heater_predictive_update(struct heater_predictive_state *state,
                         const struct heater_predictive_config *config,
                         int32_t temp_mdeg, int32_t target_mdeg,
                         int32_t ambient_mdeg, int32_t target_error_mdeg)
{
    uint32_t band = config->control_band_mdeg;
    int64_t absolute_error = target_error_mdeg;
    if (absolute_error < 0)
        absolute_error = -absolute_error;
    if (band && absolute_error > band) {
        uint16_t desired = target_error_mdeg > 0 ? config->max_output : 0;
        uint32_t max_step = config->max_output_step;
        if (max_step) {
            uint32_t low = (state->output_q16 > max_step
                            ? state->output_q16 - max_step : 0);
            uint32_t high = state->output_q16 + max_step;
            if (high > config->max_output || high < state->output_q16)
                high = config->max_output;
            if (desired < low)
                desired = low;
            else if (desired > high)
                desired = high;
        }
        state->output_q16 = desired;
        state->bias_q16 = 0;
        state->initialized = 0;
        state->rebase_output = !!desired;
        return desired;
    }
    if (!state->initialized) {
        state->filtered_temp_mdeg = temp_mdeg;
        state->initialized = 1;
    } else {
        state->filtered_temp_mdeg = filter_mdeg(
            state->filtered_temp_mdeg, temp_mdeg,
            config->observer_alpha_q15);
    }

    uint32_t retention = config->retention_q15;
    if (retention > HEATER_CONTROL_ALPHA_ONE)
        retention = HEATER_CONTROL_ALPHA_ONE;
    int64_t retained = ((int64_t)state->filtered_temp_mdeg - ambient_mdeg)
                       * retention;
    int32_t free_temp = clamp_s32((int64_t)ambient_mdeg + div_round_s64(
        retained, HEATER_CONTROL_ALPHA_ONE));
    int32_t residual_mdeg = clamp_s32((int64_t)target_mdeg - free_temp);
    if (residual_mdeg > HEATER_MODEL_TEMP_LIMIT_MDEG)
        residual_mdeg = HEATER_MODEL_TEMP_LIMIT_MDEG;
    else if (residual_mdeg < -HEATER_MODEL_TEMP_LIMIT_MDEG)
        residual_mdeg = -HEATER_MODEL_TEMP_LIMIT_MDEG;

    uint64_t response = config->response_mdeg;
    uint64_t effort = config->effort_mdeg;
    uint64_t response_sq = response * response;
    uint64_t effort_sq = effort * effort;
    uint64_t denominator = response_sq + effort_sq;
    if (!response || !denominator)
        return 0;
    int64_t numerator = (int64_t)response * residual_mdeg
                        * HEATER_CONTROL_OUTPUT_ONE
                        + (int64_t)effort_sq * state->output_q16;
    int64_t model_output = div_round_s64(numerator, denominator);

    if (state->rebase_output) {
        state->bias_q16 = (int64_t)state->output_q16 - model_output;
        state->rebase_output = 0;
    }

    int32_t error_mdeg = clamp_s32(
        (int64_t)target_mdeg - state->filtered_temp_mdeg);
    int64_t integral_delta = div_round_s64(
        (int64_t)config->integral_step_q20 * error_mdeg,
        (uint64_t)1 << HEATER_CONTROL_GAIN_SHIFT);
    int64_t max_output = config->max_output;
    int64_t bias_candidate = state->bias_q16 + integral_delta;
    if (bias_candidate < -max_output)
        bias_candidate = -max_output;
    else if (bias_candidate > max_output)
        bias_candidate = max_output;

    int64_t low = 0, high = max_output;
    uint32_t max_step = config->max_output_step;
    if (max_step) {
        low = (int64_t)state->output_q16 - max_step;
        high = (int64_t)state->output_q16 + max_step;
        if (low < 0)
            low = 0;
        if (high > max_output)
            high = max_output;
    }
    int64_t candidate = model_output + bias_candidate;
    // Apply the same directional anti-windup rule to both hard output bounds
    // and the output-movement constraint.
    if ((candidate >= low && candidate <= high)
        || (candidate > high && error_mdeg < 0)
        || (candidate < low && error_mdeg > 0))
        state->bias_q16 = bias_candidate;

    int64_t output = model_output + state->bias_q16;
    if (output < low)
        output = low;
    else if (output > high)
        output = high;
    state->output_q16 = output;
    return state->output_q16;
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
