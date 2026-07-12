// Polled UART transport for the first-class bootloader — STM32F4
// USART (RFC 0001 doc 11).
//
// See boot_uart_stm32f0.c for the transport rationale (UART over USB in
// the bootloader). The F4 USART is the older SR/DR register layout
// (not the F0/G0 ISR/TDR/RDR one), so it needs its own bring-up.
//
// USART1 on PA9 (TX) / PA10 (RX), AF7. Register-level bring-up,
// compile/link verified, not hardware-tested.

#include "stm32f407xx.h"

// USART1 lives on APB2. After reset the F4 runs from HSI (16 MHz); the
// application raises the clock, but the bootloader keeps the reset
// clock, so this is the documented nominal used for the baud divisor.
// USARTDIV = fck / baud; the mantissa/fraction split of BRR encodes it
// directly for integer divisors.
#define BOOT_UART_BAUD 250000UL
#define BOOT_UART_FCK 16000000UL
#define BOOT_UART_BRR (BOOT_UART_FCK / BOOT_UART_BAUD)

static int inited;

static void
boot_uart_init(void)
{
    if (inited)
        return;
    inited = 1;

    RCC->AHB1ENR |= RCC_AHB1ENR_GPIOAEN;
    RCC->APB2ENR |= RCC_APB2ENR_USART1EN;

    // PA9/PA10 to alternate-function mode, AF7 (USART1).
    GPIOA->MODER = (GPIOA->MODER & ~(GPIO_MODER_MODER9 | GPIO_MODER_MODER10))
        | (2u << GPIO_MODER_MODER9_Pos) | (2u << GPIO_MODER_MODER10_Pos);
    GPIOA->AFR[1] = (GPIOA->AFR[1] & ~((0xfu << 4) | (0xfu << 8)))
        | (7u << 4) | (7u << 8);

    USART1->BRR = BOOT_UART_BRR;
    USART1->CR1 = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE;
}

int
boot_link_read(uint8_t *buf, int cap)
{
    boot_uart_init();
    int n = 0;
    while (n < cap && (USART1->SR & USART_SR_RXNE))
        buf[n++] = (uint8_t)USART1->DR;
    return n;
}

int
boot_link_write(const uint8_t *buf, int len)
{
    boot_uart_init();
    for (int i = 0; i < len; i++) {
        while (!(USART1->SR & USART_SR_TXE))
            ;
        USART1->DR = buf[i];
    }
    while (!(USART1->SR & USART_SR_TC))
        ;
    return len;
}
