// Code to setup clocks on stm32f7
//
// Copyright (C) 2023  Frederic Morin <frederic.morin.8@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "autoconf.h" // CONFIG_CLOCK_REF_FREQ
#include "board/armcm_boot.h" // VectorTable
#include "board/armcm_reset.h" // try_request_canboot
#include "board/irq.h" // irq_disable
#include "board/misc.h" // bootloader_request
#include "command.h" // DECL_CONSTANT_STR
#include "internal.h" // enable_pclock
#include "sched.h" // sched_main
#if CONFIG_NEED_DMA_RESOURCE
#include "stm32/dma_mpu.h"
#endif


/****************************************************************
 * Clock setup
 ****************************************************************/

#define FREQ_PERIPH_DIV 4
#define FREQ_PERIPH (CONFIG_CLOCK_FREQ / FREQ_PERIPH_DIV)
#define FREQ_USB 48000000
#define CLOCK_WAIT_LOOPS 16000000u

enum {
    CLOCK_SOURCE_HSI = 0,
    CLOCK_SOURCE_HSE_CRYSTAL = 1,
    CLOCK_SOURCE_HSE_BYPASS = 2,
    CLOCK_SOURCE_HSI_FALLBACK = 3,
};

enum {
    CLOCK_FAULT_NONE = 0,
    CLOCK_FAULT_HSE_TIMEOUT = 1,
    CLOCK_FAULT_HSI_TIMEOUT = 2,
    CLOCK_FAULT_OVERDRIVE_TIMEOUT = 3,
    CLOCK_FAULT_OVERDRIVE_SWITCH_TIMEOUT = 4,
    CLOCK_FAULT_PLLSAI_TIMEOUT = 5,
    CLOCK_FAULT_PLL_TIMEOUT = 6,
    CLOCK_FAULT_SYSCLK_SWITCH_TIMEOUT = 7,
};

static uint8_t clock_source;
static uint8_t clock_fault;

static int
clock_wait_set(volatile uint32_t *reg, uint32_t mask)
{
    uint32_t guard = CLOCK_WAIT_LOOPS;
    while (!(*reg & mask) && --guard)
        ;
    return guard ? 0 : -1;
}

static void __attribute__((noreturn))
clock_fatal(uint8_t fault)
{
    clock_fault = fault;
    __DSB();
    NVIC_SystemReset();
    for (;;)
        ;
}

// Map a peripheral address to its enable bits
struct cline
lookup_clock_line(uint32_t periph_base)
{
    if (periph_base >= AHB1PERIPH_BASE) {
        uint32_t bit = 1 << ((periph_base - AHB1PERIPH_BASE) / 0x400);
        return (struct cline){.en=&RCC->AHB1ENR, .rst=&RCC->AHB1RSTR, .bit=bit};
    } else if (periph_base >= APB2PERIPH_BASE) {
        uint32_t bit = 1 << ((periph_base - APB2PERIPH_BASE) / 0x400);
        if (bit & 0x700)
            // Skip ADC peripheral reset as they share a bit
            return (struct cline){.en=&RCC->APB2ENR, .bit=bit};
        return (struct cline){.en=&RCC->APB2ENR, .rst=&RCC->APB2RSTR, .bit=bit};
    } else {
        uint32_t bit = 1 << ((periph_base - APB1PERIPH_BASE) / 0x400);
        return (struct cline){.en=&RCC->APB1ENR, .rst=&RCC->APB1RSTR, .bit=bit};
    }
}

// Return the frequency of the given peripheral clock
uint32_t
get_pclock_frequency(uint32_t periph_base)
{
    return FREQ_PERIPH;
}

// Enable a GPIO peripheral clock
void
gpio_clock_enable(GPIO_TypeDef *regs)
{
    uint32_t rcc_pos = ((uint32_t)regs - AHB1PERIPH_BASE) / 0x400;
    RCC->AHB1ENR |= 1 << rcc_pos;
    RCC->AHB1ENR;
}

// PLL (f765) input: 0.95 to 2.1Mhz, vco: 100 to 432Mhz, output: 24 to 216Mhz

#if !CONFIG_STM32_CLOCK_REF_INTERNAL
DECL_CONSTANT_STR("RESERVE_PINS_crystal", "PH0,PH1");
#endif

// Main clock setup called at chip startup
static void
clock_setup(void)
{
    // Configure and enable PLL
    const uint32_t pll_base = 2000000, pll_freq = CONFIG_CLOCK_FREQ * 2;
    uint32_t pllcfgr;
    if (!CONFIG_STM32_CLOCK_REF_INTERNAL) {
        // The NUCLEO-F767ZI receives an 8MHz ST-LINK MCO clock on HSE.
        // HSEBYP must be asserted before HSEON for that topology.  A fitted
        // crystal leaves bypass disabled.
#if CONFIG_STM32_HSE_BYPASS
        RCC->CR |= RCC_CR_HSEBYP;
        clock_source = CLOCK_SOURCE_HSE_BYPASS;
#else
        clock_source = CLOCK_SOURCE_HSE_CRYSTAL;
#endif
        RCC->CR |= RCC_CR_HSEON;
        if (!clock_wait_set(&RCC->CR, RCC_CR_HSERDY)) {
            const uint32_t div = CONFIG_CLOCK_REF_FREQ / pll_base;
            pllcfgr = (RCC_PLLCFGR_PLLSRC_HSE
                       | (div << RCC_PLLCFGR_PLLM_Pos));
        } else {
            // A missing ST-LINK MCO or failed crystal must not strand the
            // board before a console exists.  Recover on HSI at the same
            // configured 216MHz PLL output and report the fallback later.
            RCC->CR &= ~(RCC_CR_HSEON | RCC_CR_HSEBYP);
            RCC->CR |= RCC_CR_HSION;
            if (clock_wait_set(&RCC->CR, RCC_CR_HSIRDY))
                clock_fatal(CLOCK_FAULT_HSI_TIMEOUT);
            clock_source = CLOCK_SOURCE_HSI_FALLBACK;
            clock_fault = CLOCK_FAULT_HSE_TIMEOUT;
            const uint32_t div = 16000000 / pll_base;
            pllcfgr = (RCC_PLLCFGR_PLLSRC_HSI
                       | (div << RCC_PLLCFGR_PLLM_Pos));
        }
    } else {
        // Configure 216Mhz PLL from internal 16Mhz oscillator (HSI)
        RCC->CR |= RCC_CR_HSION;
        if (clock_wait_set(&RCC->CR, RCC_CR_HSIRDY))
            clock_fatal(CLOCK_FAULT_HSI_TIMEOUT);
        clock_source = CLOCK_SOURCE_HSI;
        const uint32_t div = 16000000 / pll_base;
        pllcfgr = RCC_PLLCFGR_PLLSRC_HSI | (div << RCC_PLLCFGR_PLLM_Pos);
    }
    RCC->PLLCFGR = (pllcfgr | ((pll_freq/pll_base) << RCC_PLLCFGR_PLLN_Pos)
                    | (0 << RCC_PLLCFGR_PLLP_Pos)  //  /2
                    | ((pll_freq/FREQ_USB) << RCC_PLLCFGR_PLLQ_Pos)
                    | (2 << RCC_PLLCFGR_PLLR_Pos));
    RCC->CR |= RCC_CR_PLLON;

    // Enable "over drive"
    enable_pclock(PWR_BASE);
    PWR->CR1 = (3 << PWR_CR1_VOS_Pos) | PWR_CR1_ODEN;
    if (clock_wait_set(&PWR->CSR1, PWR_CSR1_ODRDY))
        clock_fatal(CLOCK_FAULT_OVERDRIVE_TIMEOUT);
    PWR->CR1 = (3 << PWR_CR1_VOS_Pos) | PWR_CR1_ODEN | PWR_CR1_ODSWEN;
    if (clock_wait_set(&PWR->CSR1, PWR_CSR1_ODSWRDY))
        clock_fatal(CLOCK_FAULT_OVERDRIVE_SWITCH_TIMEOUT);

    // Enable 48Mhz USB clock
    if (CONFIG_USB) {
        // setup PLLSAI
        const uint32_t plls_base = 2000000, plls_freq = FREQ_USB * 4;
        RCC->PLLSAICFGR = (
            ((plls_freq/plls_base) << RCC_PLLSAICFGR_PLLSAIN_Pos)  // *96
            | (((plls_freq/FREQ_USB)/2 - 1) << RCC_PLLSAICFGR_PLLSAIP_Pos)// /4
            | ((plls_freq/FREQ_USB) << RCC_PLLSAICFGR_PLLSAIQ_Pos));
        // enable PLLSAI and wait for PLLSAI lock
        RCC->CR |= RCC_CR_PLLSAION;
        if (clock_wait_set(&RCC->CR, RCC_CR_PLLSAIRDY))
            clock_fatal(CLOCK_FAULT_PLLSAI_TIMEOUT);
        // set CLK48 source to PLLSAI
        RCC->DCKCFGR2 = RCC_DCKCFGR2_CK48MSEL;  // RCC_CLK48SOURCE_PLLSAIP
    }

    // Set flash latency
    MODIFY_REG(
        FLASH->ACR, FLASH_ACR_LATENCY, (uint32_t)(FLASH_ACR_LATENCY_7WS));

    // Wait for PLL lock
    if (clock_wait_set(&RCC->CR, RCC_CR_PLLRDY))
        clock_fatal(CLOCK_FAULT_PLL_TIMEOUT);

    // Switch system clock to PLL
    RCC->CFGR = RCC_CFGR_PPRE1_DIV4 | RCC_CFGR_PPRE2_DIV4 | RCC_CFGR_SW_PLL;
    uint32_t guard = CLOCK_WAIT_LOOPS;
    while ((RCC->CFGR & RCC_CFGR_SWS_Msk) != RCC_CFGR_SWS_PLL && --guard)
        ;
    if (!guard)
        clock_fatal(CLOCK_FAULT_SYSCLK_SWITCH_TIMEOUT);
}

void
command_clock_get_status(uint32_t *args)
{
    (void)args;
    sendf("clock_status source=%c fault=%c hse_bypass=%c rate=%u"
          " hse_ready=%c pll_ready=%c",
          clock_source, clock_fault, CONFIG_STM32_HSE_BYPASS,
          CONFIG_CLOCK_FREQ, !!(RCC->CR & RCC_CR_HSERDY),
          !!(RCC->CR & RCC_CR_PLLRDY));
}
DECL_COMMAND_FLAGS(command_clock_get_status, HF_IN_SHUTDOWN,
                   "clock_get_status");


/****************************************************************
 * Bootloader
 ****************************************************************/

// Handle reboot requests
void
bootloader_request(void)
{
    try_request_canboot();
    dfu_reboot();
}


/****************************************************************
 * Startup
 ****************************************************************/

// Main entry point - called from armcm_boot.c:ResetHandler()
void
armcm_main(void)
{
    // Run SystemInit() and then restore VTOR
    SystemInit();
    SCB->VTOR = (uint32_t)VectorTable;

    // Reset peripheral clocks (for some bootloaders that don't)
    RCC->AHB1ENR = 0x00100000;
    RCC->AHB2ENR = 0x00000000;
    RCC->AHB3ENR = 0x00000000;
    RCC->APB1ENR = 0x00000400;
    RCC->APB2ENR = 0x00000000;

    dfu_reboot_check();

    // STM32F7 specific DWT unlock required prior to timer_init() DWT setup.
    CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
    DWT->LAR = 0xC5ACCE55;

    clock_setup();

#if CONFIG_NEED_DMA_RESOURCE
    stm32_dma_mpu_init();
    SCB_EnableICache();
    SCB_EnableDCache();
#endif

    sched_main();
}
