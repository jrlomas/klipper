// Polled UART transport for the first-class bootloader — STM32F0/G0
// USART (RFC 0001 doc 11).
//
// Transport choice: a UART, not USB. The F072 bootloader budget is
// 16 KB (doc 11), and it already spends most of that on the intentproto
// protocol library (framing v1+v2, dictionary, extension_desc). A full
// USB-CDC stack (usbfs.c + usb_cdc.c + descriptors + enumeration) does
// not fit alongside it in 16 KB, and would add an enumeration path that
// must run before the host can even request an update. A polled UART is
// the minimal, universally-present transport: it is the same physical
// link Katapult's serial recovery uses, it needs no interrupts (so the
// bootloader vector table stays trivial), and the *identical*
// intentproto framing runs over it byte-for-byte — a host that speaks
// the protocol over serial to the application speaks it to the
// bootloader with the same code path (doc 11's core promise). USB in
// the bootloader is deferred to the dual-bank / large-flash targets
// where the budget allows it.
//
// USART1 on PA9 (TX) / PA10 (RX), AF1. This is register-level bring-up;
// it is compile/link verified here, not hardware-tested (no board in
// the build environment).

#include "stm32f072xb.h"

// Assumed USART1 kernel clock after reset-default HSI (48 MHz on F072
// via the PLL the application configures; the bootloader keeps the
// reset clock, so this is a documented nominal). BRR = fck / baud.
// 250000 baud is Klipper's serial default.
#define BOOT_UART_BAUD 250000UL
#define BOOT_UART_FCK 48000000UL
#define BOOT_UART_BRR (BOOT_UART_FCK / BOOT_UART_BAUD)

static int inited;

static void
boot_uart_init(void)
{
    if (inited)
        return;
    inited = 1;

    // Clock the GPIOA and USART1 blocks.
    RCC->AHBENR |= RCC_AHBENR_GPIOAEN;
    RCC->APB2ENR |= RCC_APB2ENR_USART1EN;

    // PA9/PA10 to alternate-function mode, AF1 (USART1).
    GPIOA->MODER = (GPIOA->MODER & ~(GPIO_MODER_MODER9 | GPIO_MODER_MODER10))
        | (2u << GPIO_MODER_MODER9_Pos) | (2u << GPIO_MODER_MODER10_Pos);
    GPIOA->AFR[1] = (GPIOA->AFR[1] & ~((0xfu << 4) | (0xfu << 8)))
        | (1u << 4) | (1u << 8);

    USART1->BRR = BOOT_UART_BRR;
    USART1->CR1 = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE;
}

// Non-blocking read: drain the RX register into buf, up to cap bytes.
// Returns the number of bytes read (0 if none available).
int
boot_link_read(uint8_t *buf, int cap)
{
    boot_uart_init();
    int n = 0;
    while (n < cap && (USART1->ISR & USART_ISR_RXNE))
        buf[n++] = (uint8_t)USART1->RDR;
    return n;
}

// Blocking write of a whole frame.
int
boot_link_write(const uint8_t *buf, int len)
{
    boot_uart_init();
    for (int i = 0; i < len; i++) {
        while (!(USART1->ISR & USART_ISR_TXE))
            ;
        USART1->TDR = buf[i];
    }
    while (!(USART1->ISR & USART_ISR_TC))
        ;
    return len;
}
