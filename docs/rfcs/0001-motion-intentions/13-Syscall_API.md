# RFC 0001: The Unified Board Syscall API

Status: Implemented in HELIX 0.9 (board syscall ABI v1.0)

## Why this exists

Klipper's ports already share a contract. Every target — STM32, ESP32,
RP2040, SAMD, AVR — implements the *same* `board/*.h` primitives:
`gpio_out_setup`, `gpio_in_read`, `gpio_adc_sample`, `gpio_pwm_write`,
`spi_transfer`, `i2c_write`, `timer_read_time`, `sched_add_timer`,
`irq_disable`, and a handful more. A command module like `gpiocmds.c`
or `neopixel.c` compiles unchanged for any of them precisely because
that contract exists.

But the contract is *implicit*. It is a scattering of header
declarations resolved at link time, with no version, no single surface,
and no way to ask a running board "what can you do?" That was fine while
the only consumers were compiled in alongside it. This fork wants more:
a world where a module can be authored on the desktop and handed to a
board that is already running — a filament sensor behavior, a custom
LED effect, a probing routine — without reflashing the whole image
([the motivating idea](#the-idea-this-came-from)). For that, the board's
call surface has to become a *thing*: named, versioned, enumerable, and
identical across families by construction rather than by convention.

That is all this document adds. One table.

## What it is

`src/generic/board_syscall.h` defines a single versioned
function-pointer table, `struct board_syscalls`, gathering the board
primitives into one place:

```c
const struct board_syscalls *bs = board_syscalls();
struct gpio_out led = bs->gpio_out_setup(pin, 0);
bs->gpio_out_write(led, 1);
uint32_t now = bs->timer_read_time();
```

* **One portable implementation** (`src/generic/board_syscall.c`) serves
  every port. It wraps the existing `board/*.h` functions — no per-family
  code, no behavior change. Because STM32 and ESP32 already satisfy the
  same `board.h`, the identical table falls out of both.
* **Versioned.** `abi_version` (`BOARD_SYSCALL_ABI_VERSION`, currently
  `1.0`) is bumped major on any incompatible layout change, minor on
  additive growth (new slots appended at the end). A consumer checks the
  version before trusting the layout.
* **Capability-advertised.** GPIO, timer, scheduler and irq are
  universal; ADC, PWM, SPI and I2C are present only when the board built
  them. `caps` is a bitmap (`BSC_CAP_*`) and the unavailable slots are
  null, so a module written against v1.0 runs on a board that has no I2C
  by checking one bit instead of failing to link.
* **Introspectable at runtime.** `query_board_syscalls` replies with the
  ABI and caps, and both are also emitted as the static dictionary
  constants `BOARD_SYSCALL_ABI` / `BOARD_SYSCALL_CAPS`, so the host sees
  the surface without a round-trip. `HELIX_STATUS` reports it per board.

It is Kconfig-gated (`WANT_SYSCALL_API`, default on where code size
allows) and additive: nothing in the existing command modules changes,
and a build without it is byte-for-byte unaffected.

## What it buys

* **Write once, run on any family.** A module targets the table, not a
  chip. The STM32/ESP32 split — the thing this fork spends the most
  effort keeping unified ([doc 12](12-ESP32_Architecture.md)) — stops
  being visible to module authors at all.
* **A negotiation surface.** The host can now ask a board what it
  supports and light up features accordingly, instead of inferring from
  the board name.
* **The substrate for pushed modules.** A relocatable module loaded at
  runtime needs exactly one thing from the firmware it lands in: a
  stable, versioned way to call hardware. This is that way. The loader,
  relocation format, and sandboxing are future work — but they now have
  a floor to stand on.

## The idea this came from

This is the surviving half of a larger proposal: a "firmware VM" that
would let modules be authored on the desktop and pushed to a board
without a rebuild. The proposal had two parts, and they had very
different value.

The **unified call surface** — this document — is unambiguously worth
building. It costs one small table, breaks nothing, and pays for itself
the moment any code wants to be family-agnostic or ask a board what it
can do.

The **bytecode virtual machine** — a language-agnostic interpreter the
firmware would *run on* — was considered and **deliberately dropped**.
On a real-time motion controller the hot path (step generation, the
timer ISR, the trajectory executor) cannot afford an interpreter between
it and the hardware; anything pushed at runtime must be confined to the
cold path, where the cost of a VM buys little over simply calling
syscalls directly. If a runtime-loaded extension mechanism is ever
built, the right shape is a sandboxed, cold-path-only module format
(a Wasm/eBPF-style verified blob) that calls *this* table — not a VM the
whole firmware executes inside. That remains an open, post-hardware
question. What is settled is that the valuable, low-risk foundation
lands now and the speculative interpreter does not.
