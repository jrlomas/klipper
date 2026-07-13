// Window Comparator functions on STM32G0B1, using COMP1/COMP2 and
// COMP2/COMP3 peripherals.
// Implements window comparators that trigger when input voltage is within
// a specified range defined by upper and lower thresholds.
// Supports PA1 (COMP1/COMP2) and PA3 (COMP2/COMP3) configurations.
// Features IRQ management to prevent continuous triggering when outside
// thresholds.
//
// Copyright (C) 2025 JR Lomas (discord:knight_rad.iant) <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "command.h" // DECL_COMMAND, shutdown
#include "basecmd.h" // oid_alloc
#include "sched.h" // sched_add_timer, DECL_INIT
#include "board/irq.h" // irq_disable
#include "board/misc.h" // timer_read_time
#include "board/gpio.h" // gpio_in_setup, gpio_in_read
#include "board/armcm_boot.h" // DECL_ARMCM_IRQ
#include "stm32/internal.h" // GPIO, GPIO2PORT, GPIO2BIT
#include "compiler.h" // container_of
#include "trigger_source.h" // trigger_source_alloc
#include <string.h> // memset
#include <stdint.h> // uintptr_t
#include <stdlib.h>

#if CONFIG_MACH_STM32G0

typedef struct {
    uint8_t oid;
    uint32_t gpio_pin;
    uint8_t comp_pair;  // 0 = COMP1/COMP2 (PA1), 1 = COMP2/COMP3 (PA3)
    uint16_t upper_threshold; // 0-4095 DAC value for upper threshold
    uint16_t lower_threshold; // 0-4095 DAC value for lower threshold
} comp_config_t;

static comp_config_t *comp_config = NULL;

// Initialize DAC for threshold setting
static void setup_dac(uint8_t dac_channel, uint16_t value) {
    // Enable DAC clock
    RCC->APBENR1 |= RCC_APBENR1_DAC1EN;

    // Clamp to 12 bits
    value &= 0x0FFF;

    // Configure DAC channel: enable first, then write DHR and trigger
    // Select internal-only routing to avoid driving external PA4/PA5.
    // Set MODE1 and MODE2 to a non-zero value that routes internal outputs to
    // internal peripherals (see reference manual for exact MODE values).
     /* Clear then set MODE fields for both channels to 0b111 using header
        constants. This routes to on-chip peripherals without driving PA4/PA5.
     */
     DAC1->MCR &= ~(DAC_MCR_MODE1_Msk | DAC_MCR_MODE2_Msk);
     DAC1->MCR |= (DAC_MCR_MODE1_0 | DAC_MCR_MODE1_1 | DAC_MCR_MODE1_2 |
                        DAC_MCR_MODE2_0 | DAC_MCR_MODE2_1 | DAC_MCR_MODE2_2);
    if (dac_channel == 1) {
        // DAC Channel 1 - PA4
        DAC1->CR |= DAC_CR_EN1;
        DAC1->DHR12R1 = value; // 12-bit value
        // Trigger the DAC conversion now (software trigger)
        // meaning output the given value as voltage
        DAC1->SWTRIGR |= DAC_SWTRIGR_SWTRIG1;
    } else if (dac_channel == 2) {
        // DAC Channel 2 - PA5
        DAC1->CR |= DAC_CR_EN2;
        DAC1->DHR12R2 = value; // 12-bit value
        // Trigger the DAC conversion now (software trigger)
        // meaning output the given value as voltage
        DAC1->SWTRIGR |= DAC_SWTRIGR_SWTRIG2;
    }
}

#define HYSTERESIS_NONE 0b00
#define HYSTERESIS_LOW 0b01
#define HYSTERESIS_MEDIUM 0b10
#define HYSTERESIS_HIGH 0b11

#define HYSTERESIS_SETTING HYSTERESIS_HIGH

// Configure COMP as window comparator (internal triggering only)
static void
setup_comp_window_comparator(uint32_t input_pin, uint16_t upper_threshold,
                             uint16_t lower_threshold)
{
    // Enable COMP clock via SYSCFG
    RCC->APBENR2 |= RCC_APBENR2_SYSCFGEN;

    if (input_pin == GPIO('A', 1)) {
        // Configure COMP1/COMP2 window comparator with PA1 input
        gpio_peripheral(GPIO('A', 1), GPIO_ANALOG, 0); // INP


        // Configure COMP1 (upper threshold comparator)
        uint32_t csr1 = 0;

        // PA1 input, DAC channel 1 threshold, single mode, XOR output,
        // no blanking or polarity inversion, high-speed operation.
        csr1 |= ((0b10 << COMP_CSR_INPSEL_Pos)
                 & COMP_CSR_INPSEL_Msk);
        csr1 |= ((0b100 << COMP_CSR_INMSEL_Pos)
                 & COMP_CSR_INMSEL_Msk);
        csr1 |= ((0b0 << COMP_CSR_WINMODE_Pos)
                 & COMP_CSR_WINMODE_Msk);
        csr1 |= ((0b1 << COMP_CSR_WINOUT_Pos)
                 & COMP_CSR_WINOUT_Msk);
        csr1 |= ((0b0 << COMP_CSR_BLANKING_Pos)
                 & COMP_CSR_BLANKING_Msk);
        csr1 |= ((0b0 << COMP_CSR_POLARITY_Pos)
                 & COMP_CSR_POLARITY_Msk);
        csr1 |= ((0b00 << COMP_CSR_PWRMODE_Pos)
                 & COMP_CSR_PWRMODE_Msk);
        csr1 |= ((HYSTERESIS_SETTING << COMP_CSR_HYST_Pos)
                 & COMP_CSR_HYST_Msk);
    // Do NOT enable COMP1 here; enable after EXTI/NVIC is configured
    COMP1->CSR = csr1;

        // Configure COMP2 (lower threshold comparator)
        uint32_t csr2 = 0;

        // PA3 selection is overwritten by window mode; use DAC channel 2,
        // inverted polarity, no blanking, and high-speed operation.
        csr2 |= ((0b10 << COMP_CSR_INPSEL_Pos)
                 & COMP_CSR_INPSEL_Msk);
        csr2 |= ((0b0101 << COMP_CSR_INMSEL_Pos)
                 & COMP_CSR_INMSEL_Msk);
        csr2 |= ((0b1 << COMP_CSR_WINMODE_Pos)
                 & COMP_CSR_WINMODE_Msk);
        csr2 |= ((0b0 << COMP_CSR_WINOUT_Pos)
                 & COMP_CSR_WINOUT_Msk);
        csr2 |= ((0b0 << COMP_CSR_BLANKING_Pos)
                 & COMP_CSR_BLANKING_Msk);
        csr2 |= ((0b1 << COMP_CSR_POLARITY_Pos)
                 & COMP_CSR_POLARITY_Msk);
        csr2 |= ((0b00 << COMP_CSR_PWRMODE_Pos)
                 & COMP_CSR_PWRMODE_Msk);
        csr2 |= ((HYSTERESIS_SETTING << COMP_CSR_HYST_Pos)
                 & COMP_CSR_HYST_Msk);
    // Do NOT enable COMP2 here; enable after EXTI/NVIC is configured
    COMP2->CSR = csr2;

    } else if (input_pin == GPIO('A', 3)) {
        // Configure COMP2/COMP3 window comparator with PA3 input
        gpio_peripheral(GPIO('A', 3), GPIO_ANALOG, 0); // INP

        // Configure COMP2 (upper threshold comparator)
        uint32_t csr2 = 0;

        // PA3 input, DAC channel 1 threshold, single mode, XOR output,
        // no blanking or polarity inversion, high-speed operation.
        csr2 |= ((0b10 << COMP_CSR_INPSEL_Pos)
                 & COMP_CSR_INPSEL_Msk);
        csr2 |= ((0b100 << COMP_CSR_INMSEL_Pos)
                 & COMP_CSR_INMSEL_Msk);
        csr2 |= ((0b0 << COMP_CSR_WINMODE_Pos)
                 & COMP_CSR_WINMODE_Msk);
        csr2 |= ((0b1 << COMP_CSR_WINOUT_Pos)
                 & COMP_CSR_WINOUT_Msk);
        csr2 |= ((0b0 << COMP_CSR_BLANKING_Pos)
                 & COMP_CSR_BLANKING_Msk);
        csr2 |= ((0b0 << COMP_CSR_POLARITY_Pos)
                 & COMP_CSR_POLARITY_Msk);
        csr2 |= ((0b00 << COMP_CSR_PWRMODE_Pos)
                 & COMP_CSR_PWRMODE_Msk);
        csr2 |= ((HYSTERESIS_SETTING << COMP_CSR_HYST_Pos)
                 & COMP_CSR_HYST_Msk);
    // Do NOT enable COMP2 here; enable after EXTI/NVIC is configured
    COMP2->CSR = csr2;

        // Configure COMP3 (lower threshold comparator)
        uint32_t csr3 = 0;
        // PB0 selection is overwritten by window mode; use DAC channel 2,
        // inverted polarity, no blanking, and high-speed operation.
        csr3 |= ((0b00 << COMP_CSR_INPSEL_Pos)
                 & COMP_CSR_INPSEL_Msk);
        csr3 |= ((0b0101 << COMP_CSR_INMSEL_Pos)
                 & COMP_CSR_INMSEL_Msk);
        csr3 |= ((0b1 << COMP_CSR_WINMODE_Pos)
                 & COMP_CSR_WINMODE_Msk);
        csr3 |= ((0b0 << COMP_CSR_WINOUT_Pos)
                 & COMP_CSR_WINOUT_Msk);
        csr3 |= ((0b0 << COMP_CSR_BLANKING_Pos)
                 & COMP_CSR_BLANKING_Msk);
        csr3 |= ((0b1 << COMP_CSR_POLARITY_Pos)
                 & COMP_CSR_POLARITY_Msk);
        csr3 |= ((0b00 << COMP_CSR_PWRMODE_Pos)
                 & COMP_CSR_PWRMODE_Msk);
        csr3 |= ((HYSTERESIS_SETTING << COMP_CSR_HYST_Pos)
                 & COMP_CSR_HYST_Msk);
    // Do NOT enable COMP3 here; enable after EXTI/NVIC is configured
    COMP3->CSR = csr3;

    } else {
        shutdown("Window comparator input must be PA1 or PA3");
        return;
    }
}

// Enable the pair after EXTI/NVIC setup, as required by the reference sequence.
static void enable_comp_pair(uint32_t input_pin) {
    if (input_pin == GPIO('A', 1)) {
        COMP1->CSR |= COMP_CSR_EN_Msk;
        COMP2->CSR |= COMP_CSR_EN_Msk;
    } else if (input_pin == GPIO('A', 3)) {
        COMP2->CSR |= COMP_CSR_EN_Msk;
        COMP3->CSR |= COMP_CSR_EN_Msk;
    }
}

// Map COMP output to EXTI line for interrupt generation (window comparator)
static void setup_comp_window_exti(uint32_t input_pin) {
    irqstatus_t flag = irq_save();

    if (input_pin == GPIO('A', 1)) {
        // For COMP1/COMP2 window comparator
        // COMP1 → EXTI17, COMP2 → EXTI18
        uint32_t exti17_mask = (1U << 17); // COMP1
        uint32_t exti18_mask = (1U << 18); // COMP2

        // Clear any pending interrupts
        EXTI->RPR1 = exti17_mask | exti18_mask;
        EXTI->FPR1 = exti17_mask | exti18_mask;

        EXTI->RTSR1 |= exti17_mask;      // COMP1 rising
        EXTI->RTSR1 |= exti18_mask;      // COMP2 rising
        // Falling edges too, so entering the window (both outputs
        // dropping to 0) also raises an event for trigger sources
        EXTI->FTSR1 |= exti17_mask | exti18_mask;

        // Enable interrupts
        EXTI->IMR1 |= exti17_mask | exti18_mask;

    } else if (input_pin == GPIO('A', 3)) {
        // For COMP2/COMP3 window comparator
        // COMP2 → EXTI18, COMP3 → EXTI20
        uint32_t exti18_mask = (1U << 18); // COMP2
        uint32_t exti20_mask = (1U << 20); // COMP3

        // Clear any pending interrupts
        EXTI->RPR1 = exti18_mask | exti20_mask;
        EXTI->FPR1 = exti18_mask | exti20_mask;

        EXTI->RTSR1 |= exti18_mask;      // COMP2 rising
        EXTI->RTSR1 |= exti20_mask;      // COMP3 rising
        // Falling edges too, so entering the window also interrupts
        EXTI->FTSR1 |= exti18_mask | exti20_mask;

        // Enable interrupts
        EXTI->IMR1 |= exti18_mask | exti20_mask;
    }

    // Enable NVIC interrupt for ADC1_COMP with low priority (3)
    NVIC_SetPriority(ADC1_COMP_IRQn, 3);
    NVIC_EnableIRQ(ADC1_COMP_IRQn);

    irq_restore(flag);
}

// Trigger-source bridge (FD-0001 doc 09): a window comparator can
// drive trsync directly as a hardware trigger source. Configured by
// config_trigger_comp; fires when the input enters (on_enter=1) or
// leaves (on_enter=0) the window.
static struct trigger_source *comp_tsrc;
static uint8_t comp_tsrc_on_enter;
static uint8_t comp_last_in_window;

static uint8_t read_window_comp_value(uint32_t input_pin);

static void comp_trigger_deliver(uint32_t clock) {
    if (!comp_tsrc || !comp_config)
        return;
    uint8_t in_window = read_window_comp_value(comp_config->gpio_pin);
    uint8_t was = comp_last_in_window;
    comp_last_in_window = in_window;
    if (in_window == was)
        return;
    if (in_window == comp_tsrc_on_enter)
        trigger_source_notify(comp_tsrc, clock);
}

// Handle window comparator interrupts via EXTI lines
void WindowComparatorIRQHandler(void) {
    uint32_t clock = timer_read_time();
    uint32_t exti17_mask = (1U << 17); // COMP1
    uint32_t exti18_mask = (1U << 18); // COMP2
    uint32_t exti20_mask = (1U << 20); // COMP3

    // Clear falling-edge pendings (used only by the trigger bridge)
    EXTI->FPR1 = exti17_mask | exti18_mask | exti20_mask;
    comp_trigger_deliver(clock);

    // Check if any comparator triggered an interrupt
    if ((EXTI->RPR1) & (exti17_mask | exti18_mask | exti20_mask)) {

        if (comp_config->comp_pair == 0) { // COMP1/COMP2 pair
            EXTI->RPR1 = exti17_mask | exti18_mask;

            // Read both comparator outputs
            uint8_t comp1_out = (COMP1->CSR & COMP_CSR_VALUE) ? 1 : 0;
            uint8_t comp2_out = (COMP2->CSR & COMP_CSR_VALUE) ? 1 : 0;

            // Check which comparator triggered
            if (comp1_out == 1) {
                // Upper threshold exceeded
                sendf("comp_upper_trigger pin=%u", comp_config->gpio_pin);
            }

            if (comp2_out == 1) {
                // Below lower threshold
                sendf("comp_lower_trigger pin=%u", comp_config->gpio_pin);
            }

        } else if (comp_config->comp_pair == 1) { // COMP2/COMP3 pair
            EXTI->RPR1 = exti18_mask | exti20_mask;

            // Read both comparator outputs
            uint8_t comp2_out = (COMP2->CSR & COMP_CSR_VALUE) ? 1 : 0;
            uint8_t comp3_out = (COMP3->CSR & COMP_CSR_VALUE) ? 1 : 0;

            //sendf("comp comp2_out=%u comp3_out=%u", comp2_out, comp3_out);

            // Check which comparator triggered
            if (comp2_out == 1) {
                // Upper threshold exceeded
                sendf("comp_upper_trigger pin=%u", comp_config->gpio_pin);
            }

            if (comp3_out == 1) {
                // Below lower threshold
                sendf("comp_lower_trigger pin=%u", comp_config->gpio_pin);
            }
        }
    }
}

/* Read window comparator state. Returns 1 if within window, 0 if outside. */
static uint8_t read_window_comp_value(uint32_t input_pin) {
    if (input_pin == GPIO('A', 1)) {
        // COMP1/COMP2 window comparator
        uint8_t comp1_out = (COMP1->CSR & COMP_CSR_VALUE) ? 1 : 0;
        uint8_t comp2_out = (COMP2->CSR & COMP_CSR_VALUE) ? 1 : 0;
        return (comp1_out == 0 && comp2_out == 0) ? 1 : 0;
    } else if (input_pin == GPIO('A', 3)) {
        // COMP2/COMP3 window comparator
        uint8_t comp2_out = (COMP2->CSR & COMP_CSR_VALUE) ? 1 : 0;
        uint8_t comp3_out = (COMP3->CSR & COMP_CSR_VALUE) ? 1 : 0;
        return (comp2_out == 0 && comp3_out == 0) ? 1 : 0;
    }
    return 0;
}

void init_comp(void) {
    // Enable necessary clocks
    RCC->APBENR1 |= RCC_APBENR1_DAC1EN;
    RCC->APBENR2 |= RCC_APBENR2_SYSCFGEN;
}
DECL_INIT(init_comp);

void command_config_comp(uint32_t *args) {
    if (comp_config != NULL)
        shutdown("Only one window comparator is supported");

    comp_config = oid_alloc(args[0], command_config_comp, sizeof(*comp_config));
    comp_config->oid = args[0];
    comp_config->gpio_pin = args[1];
    comp_config->upper_threshold = args[2];
    comp_config->lower_threshold = args[3];

    // Determine comparator pair based on input pin
    if (comp_config->gpio_pin == GPIO('A', 1)) {
        comp_config->comp_pair = 0; // COMP1/COMP2 pair
    } else if (comp_config->gpio_pin == GPIO('A', 3)) {
        comp_config->comp_pair = 1; // COMP2/COMP3 pair
    } else {
        shutdown("Window comparator input must be PA1 or PA3");
    }

    // Validate thresholds
    if (comp_config->upper_threshold <= comp_config->lower_threshold) {
        shutdown("Upper threshold must be greater than lower threshold");
    }

    // DAC1 channels 1 and 2 provide the upper and lower thresholds.
    setup_dac(1, comp_config->upper_threshold);
    setup_dac(2, comp_config->lower_threshold);

    // Setup window comparator
    setup_comp_window_comparator(comp_config->gpio_pin,
                                 comp_config->upper_threshold,
                                 comp_config->lower_threshold);

    // Setup EXTI interrupts for window comparator
    setup_comp_window_exti(comp_config->gpio_pin);

    // Enable the pair after EXTI/NVIC setup, per the reference sequence.
    enable_comp_pair(comp_config->gpio_pin);
}
DECL_COMMAND(command_config_comp,
             "config_comp oid=%c pin=%u upper=%u lower=%u");

// Command to query current comparator states and IRQ status
void command_comp_query_state(uint32_t *args) {
    // Not used, as only one comparator is supported
    //uint8_t oid = args[0];

    // Find the configuration by OID
    comp_config_t *comp = comp_config;
    if (!comp) {
        shutdown("Invalid comp OID");
        return;
    }

    // Read current comparator states
    uint8_t in_window = read_window_comp_value(comp->gpio_pin);
    uint8_t upper_comp_out, lower_comp_out;

    if (comp->comp_pair == 0) {
        // COMP1/COMP2 pair
        upper_comp_out = (COMP1->CSR & COMP_CSR_VALUE) ? 1 : 0;
        lower_comp_out = (COMP2->CSR & COMP_CSR_VALUE) ? 1 : 0;
    } else {
        // COMP2/COMP3 pair
        upper_comp_out = (COMP2->CSR & COMP_CSR_VALUE) ? 1 : 0;
        lower_comp_out = (COMP3->CSR & COMP_CSR_VALUE) ? 1 : 0;
    }

    sendf("comp_state oid=%c pin=%u in_window=%c upper_out=%c lower_out=%c",
          comp->oid, comp->gpio_pin, in_window, upper_comp_out, lower_comp_out);
}
DECL_COMMAND(command_comp_query_state, "comp_query_state oid=%c");

// Register the window comparator as a trsync trigger source
// (armed/disarmed via the generic trigger_source_arm commands).
void command_config_trigger_comp(uint32_t *args) {
    if (!comp_config)
        shutdown("config_comp must precede config_trigger_comp");
    if (comp_tsrc)
        shutdown("comp trigger source already configured");
    struct trigger_source *tsrc = trigger_source_alloc(
        args[0], TS_KIND_COMP);
    comp_tsrc_on_enter = !!args[1];
    comp_last_in_window = read_window_comp_value(comp_config->gpio_pin);
    // Comparator events always flow (hardware hysteresis debounces);
    // the generic ARMED flag gates trsync delivery.
    tsrc->hw_arm = NULL;
    comp_tsrc = tsrc;
}
DECL_COMMAND(command_config_trigger_comp,
             "config_trigger_comp oid=%c on_enter=%c");

/*
 * Register the combined ADC1/COMP IRQ for STM32G0 variants here so that the
 * comparator EXTI handling (comp_handle_comp_irqs) is wired up in the same
 * compilation unit as the comparator/DAC setup. This avoids duplicate IRQ
 * registrations when different files try to claim ADC1_COMP_IRQn.
 */
#if CONFIG_MACH_STM32G0Bx || (CONFIG_MACH_STM32G0 && defined(ADC1_COMP_IRQn))
DECL_ARMCM_IRQ(WindowComparatorIRQHandler,ADC1_COMP_IRQn);
#endif

#else
void init_comp(void) {
}
DECL_INIT(init_comp);

void command_config_comp(uint32_t *args) {
    shutdown("COMP not supported on this chip");
}
DECL_COMMAND(command_config_comp,
             "config_comp oid=%c pin=%u upper=%u lower=%u");

void command_comp_set_irq(uint32_t *args) {
    shutdown("IRQ management removed - fixed edge triggering enabled");
}
DECL_COMMAND(command_comp_set_irq,
             "comp_set_irq oid=%c upper_enable=%c lower_enable=%c");

void command_comp_reset_irq(uint32_t *args) {
    shutdown("IRQ management removed - fixed edge triggering enabled");
}
DECL_COMMAND(command_comp_reset_irq, "comp_reset_irq oid=%c");

void command_comp_query_state(uint32_t *args) {
    shutdown("COMP not supported on this chip");
}
DECL_COMMAND(command_comp_query_state, "comp_query_state oid=%c");

void command_config_trigger_comp(uint32_t *args) {
    shutdown("COMP not supported on this chip");
}
DECL_COMMAND(command_config_trigger_comp,
             "config_trigger_comp oid=%c on_enter=%c");

#endif
