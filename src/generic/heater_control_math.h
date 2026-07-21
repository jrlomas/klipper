#ifndef __HEATER_CONTROL_MATH_H
#define __HEATER_CONTROL_MATH_H

#include <stdint.h>

#define HEATER_CONTROL_OUTPUT_ONE 65535u
#define HEATER_CONTROL_GAIN_SHIFT 20
#define HEATER_CONTROL_ALPHA_ONE 32768u
#define HEATER_CONTROL_MODEL_SHIFT 15

struct heater_pid_config {
    // Parallel PID gains in Q20 output units.  ki_step and kd_step already
    // include the fixed controller period: Ki*dt and Kd/dt respectively.
    int32_t kp_q20;
    int32_t ki_step_q20;
    int32_t kd_step_q20;
    uint16_t derivative_alpha_q15;
    uint16_t max_output;
};

struct heater_pid_state {
    int64_t integral_q20;
    int64_t output_q20;
    int32_t previous_temp_mdeg;
    int32_t derivative_mdeg;
    uint8_t initialized;
};

// Closed-form, single-input predictive thermal controller.  The host turns a
// first-order plant model and prediction horizon into retention and response
// coefficients.  Firmware therefore performs no floating-point model fit or
// online matrix solve.
struct heater_predictive_config {
    // Predicted retained fraction of (temperature - ambient), Q15.
    uint16_t retention_q15;
    // EWMA coefficient applied to the control temperature, Q15.
    uint16_t observer_alpha_q15;
    uint16_t max_output;
    // Maximum Q16 duty change per control update; zero means unrestricted.
    uint16_t max_output_step;
    // Predicted milli-degree rise over the horizon at full duty.
    uint32_t response_mdeg;
    // Equivalent milli-degree cost assigned to a full-duty output change.
    uint32_t effort_mdeg;
    // Predict only while the target-local sensor tangent reports an absolute
    // error inside this band. Outside it use slew-bounded approach control.
    uint32_t control_band_mdeg;
    // Q20 Q16-duty increment per milli-degree error per update.
    int32_t integral_step_q20;
};

struct heater_predictive_state {
    int64_t bias_q16;
    int32_t filtered_temp_mdeg;
    uint16_t output_q16;
    uint8_t initialized;
    uint8_t rebase_output;
};

struct heater_verify_config {
    uint32_t period_ms;
    uint32_t hysteresis_mdeg;
    uint32_t max_error_mdeg_ms;
    uint32_t heating_gain_mdeg;
    uint32_t gain_samples;
};

struct heater_verify_state {
    int64_t error_mdeg_ms;
    int32_t goal_mdeg;
    uint32_t remaining_samples;
    uint8_t approaching;
    uint8_t starting;
    uint8_t new_target;
};

// Convert one raw ADC observation into target-temperature error.  The host
// supplies the local sensor slope at the target in Q16 milli-degrees C/count.
int32_t heater_control_adc_error_mdeg(uint32_t adc, uint32_t target_adc,
                                     int32_t slope_q16);

void heater_pid_reset(struct heater_pid_state *state);

// Retain the current output across a gain change, while clearing the filtered
// derivative so the new Kd cannot amplify history accumulated under another
// profile.  The next update resumes normal conditional integration.
void heater_pid_reconfigure(struct heater_pid_state *state,
                            const struct heater_pid_config *config,
                            int32_t error_mdeg);

// Run one deterministic, fixed-period PID update.  The derivative is taken
// from the measurement (not the error), preventing target changes from
// producing derivative kick.  Returns a Q16 duty in [0, max_output].
uint16_t heater_pid_update(struct heater_pid_state *state,
                           const struct heater_pid_config *config,
                           int32_t temp_mdeg, int32_t error_mdeg);

void heater_predictive_reset(struct heater_predictive_state *state);

// Preserve the current output when model coefficients change.  The next
// observation rebases the signed model-error bias around the new prediction.
void heater_predictive_reconfigure(struct heater_predictive_state *state);

// Minimize, in closed form, predicted temperature error plus an explicit
// output-movement cost, then apply integral model-error correction and an
// independent output slew bound.  All temperatures are milli-degrees C.
uint16_t heater_predictive_update(
    struct heater_predictive_state *state,
    const struct heater_predictive_config *config,
    int32_t temp_mdeg, int32_t target_mdeg, int32_t ambient_mdeg,
    int32_t target_error_mdeg);

void heater_verify_reset(struct heater_verify_state *state,
                         uint8_t new_target);

// Mirror Klipper's verify_heater progress/error state machine at the local
// controller cadence. Returns non-zero only when heat is no longer making
// progress and the accumulated error budget has subsequently expired.
uint8_t heater_verify_update(struct heater_verify_state *state,
                             const struct heater_verify_config *config,
                             int32_t temp_mdeg, uint8_t target_valid,
                             int32_t target_mdeg);

#endif // __HEATER_CONTROL_MATH_H
