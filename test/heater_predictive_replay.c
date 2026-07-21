#include <stdint.h>
#include <stdio.h>

#include "src/generic/heater_control_math.h"

int
main(void)
{
    struct heater_predictive_config config = {
        .retention_q15 = 26828,
        .observer_alpha_q15 = 4564,
        .max_output = HEATER_CONTROL_OUTPUT_ONE,
        .max_output_step = 19661,
        .response_mdeg = 13622,
        .effort_mdeg = 4000,
        .control_band_mdeg = 1000,
        .integral_step_q20 = 10308,
    };
    struct heater_predictive_state state;
    heater_predictive_reset(&state);
    int32_t temperature;
    while (scanf("%d", &temperature) == 1) {
        uint16_t output = heater_predictive_update(
            &state, &config, temperature, 75000, 28090,
            75000 - temperature);
        printf("%u\n", output);
    }
    return 0;
}
