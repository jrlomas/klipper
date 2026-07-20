#include <assert.h>
#include <stdint.h>
#include <stdio.h>

#include "src/generic/heater_control_math.h"

#define Q20(v) ((int32_t)((v) * (1 << HEATER_CONTROL_GAIN_SHIFT) + .5))

static void
test_adc_slope(void)
{
    // NTC divider: ADC falls as temperature rises.
    int32_t slope = -100 * (1 << 16); // -100 mC/count in Q16
    assert(heater_control_adc_error_mdeg(2100, 2000, slope) == 10000);
    assert(heater_control_adc_error_mdeg(1900, 2000, slope) == -10000);
}

static void
test_proportional_and_bounds(void)
{
    struct heater_pid_config cfg = {
        .kp_q20 = Q20(.020),
        .ki_step_q20 = 0,
        .kd_step_q20 = 0,
        .derivative_alpha_q15 = HEATER_CONTROL_ALPHA_ONE,
        .max_output = HEATER_CONTROL_OUTPUT_ONE,
    };
    struct heater_pid_state state;
    heater_pid_reset(&state);
    uint16_t out = heater_pid_update(&state, &cfg, 20000, 10000);
    assert(out > 13000 && out < 13250); // 20% duty
    out = heater_pid_update(&state, &cfg, 20000, -10000);
    assert(out == 0);
    out = heater_pid_update(&state, &cfg, 20000, 100000);
    assert(out == HEATER_CONTROL_OUTPUT_ONE);
}

static void
test_derivative_on_measurement(void)
{
    struct heater_pid_config cfg = {
        .kp_q20 = 0,
        .ki_step_q20 = 0,
        .kd_step_q20 = Q20(.010),
        .derivative_alpha_q15 = HEATER_CONTROL_ALPHA_ONE,
        .max_output = HEATER_CONTROL_OUTPUT_ONE,
    };
    struct heater_pid_state state;
    heater_pid_reset(&state);
    assert(heater_pid_update(&state, &cfg, 100000, 10000) == 0);
    // A target-only error change has no derivative kick.
    assert(heater_pid_update(&state, &cfg, 100000, 20000) == 0);
    // A falling measurement contributes positive heater output.
    assert(heater_pid_update(&state, &cfg, 90000, 20000) > 6500);
}

static void
test_anti_windup_and_recovery(void)
{
    struct heater_pid_config cfg = {
        .kp_q20 = Q20(.200),
        .ki_step_q20 = Q20(.020),
        .kd_step_q20 = 0,
        .derivative_alpha_q15 = HEATER_CONTROL_ALPHA_ONE,
        .max_output = HEATER_CONTROL_OUTPUT_ONE,
    };
    struct heater_pid_state state;
    heater_pid_reset(&state);
    for (int i = 0; i < 100; i++)
        assert(heater_pid_update(&state, &cfg, 20000, 10000)
               == HEATER_CONTROL_OUTPUT_ONE);
    assert(state.integral_q20 == 0); // saturated positive error did not wind up
    assert(heater_pid_update(&state, &cfg, 20000, -1000) == 0);
}

static void
test_bumpless_reconfigure(void)
{
    struct heater_pid_config cfg = {
        .kp_q20 = Q20(.020), .ki_step_q20 = Q20(.005),
        .kd_step_q20 = 0,
        .derivative_alpha_q15 = HEATER_CONTROL_ALPHA_ONE,
        .max_output = HEATER_CONTROL_OUTPUT_ONE,
    };
    struct heater_pid_state state;
    heater_pid_reset(&state);
    uint16_t before = heater_pid_update(&state, &cfg, 100000, 10000);
    cfg.kp_q20 = Q20(.010);
    heater_pid_reconfigure(&state, &cfg, 10000);
    uint16_t after = heater_pid_update(&state, &cfg, 100000, 10000);
    // The integral is retargeted so changing Kp does not collapse duty.  The
    // only increase is the ordinary next-sample Ki contribution (about 5%).
    assert(after >= before && after - before < 3400);
    assert(state.derivative_mdeg == 0);
}

static void
test_verify_heater_progress_and_stall(void)
{
    struct heater_verify_config cfg = {
        .period_ms = 300,
        .hysteresis_mdeg = 5000,
        .max_error_mdeg_ms = 120000000,
        .heating_gain_mdeg = 2000,
        .gain_samples = 200,
    };
    struct heater_verify_state state;
    heater_verify_reset(&state, 1);
    // A normal rise repeatedly earns another progress window and never spends
    // the accumulated-error budget merely because it starts far from target.
    for (int32_t temp = 26000; temp < 56000; temp += 500)
        assert(!heater_verify_update(&state, &cfg, temp, 1, 60000));
    assert(!state.error_mdeg_ms);

    // A stalled heater first consumes its permitted progress window. Only
    // after that window expires may accumulated error latch a fault.
    heater_verify_reset(&state, 1);
    for (uint32_t i = 0; i <= cfg.gain_samples; i++)
        assert(!heater_verify_update(&state, &cfg, 26000, 1, 60000));
    uint8_t fault = 0;
    for (uint32_t i = 0; i < 100 && !fault; i++)
        fault = heater_verify_update(&state, &cfg, 26000, 1, 60000);
    assert(fault);

    // Entering the target band clears both progress and accumulated error.
    assert(!heater_verify_update(&state, &cfg, 56000, 1, 60000));
    assert(!state.error_mdeg_ms && !state.approaching);
}

int
main(void)
{
    test_adc_slope();
    test_proportional_and_bounds();
    test_derivative_on_measurement();
    test_anti_windup_and_recovery();
    test_bumpless_reconfigure();
    test_verify_heater_progress_and_stall();
    puts("PASS: autonomous heater controller fixed-point math");
    return 0;
}
