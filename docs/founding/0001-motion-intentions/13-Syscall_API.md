# FD-0001: The Unified Board Syscall API

Status: Implemented in HELIX 0.9 (board syscall ABI v1.0). It is the
low-level substrate for the planned
[target-native module architecture](24-Target_Native_Machine_Modules.md), not
the capability boundary exposed directly to ordinary machine applications.

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

* **Write once, compile for each qualified family.** Portable module source
  targets this semantic floor, not one chip's registers. The STM32/ESP32 split
  — the thing this fork spends the most effort keeping unified
  ([doc 12](12-ESP32_Architecture.md)) — stops being visible to authors even
  though the deployed native binaries remain target-specific.
* **A negotiation surface.** The host can now ask a board what it
  supports and light up features accordingly, instead of inferring from
  the board name.
* **The substrate for pushed modules.** A relocatable module loaded at
  runtime needs exactly one thing from the firmware it lands in: a
  stable, versioned way to call hardware. This is that way. The loader,
  native container, capability-scoped application API, and isolation are
  specified in
  [24-Target_Native_Machine_Modules.md](24-Target_Native_Machine_Modules.md).
  Those application APIs sit above this table; an ordinary job module does
  not receive raw GPIO setup or interrupt-control authority.

## The idea this came from

This began as the surviving half of a larger "firmware VM" proposal that
would let modules be authored on the desktop and pushed to a board without a
whole-firmware rebuild. The unified call surface landed first because it had
independent value and forced the cross-family boundary to become explicit.

The **unified call surface** — this document — is unambiguously worth
building. It costs one small table, breaks nothing, and pays for itself
the moment any code wants to be family-agnostic or ask a board what it
can do.

The **bytecode virtual machine** — a language-agnostic interpreter the
firmware would *run on* — remains deliberately rejected. On a real-time
motion controller the hot path cannot afford a universal interpreter between
an algorithm and its deadline.

The selected evolution is instead **target-native loading**. Restricted,
typed source is compiled on the workstation for the actual target; the
printer stores, verifies, relocates, and executes those native instructions
without reflashing the kernel. A semantic machine API constrains ordinary
applications, while separately qualified hard-real-time control domains
support bounded algorithms such as BLDC/FOC. The raw board syscall table
remains a privileged kernel/system-extension substrate and is not itself the
sandbox.

That complete decision, including the `.hmod` format, loader, target classes,
MPU limitations, lifecycle, and physical gates, is recorded in
[24-Target_Native_Machine_Modules.md](24-Target_Native_Machine_Modules.md).
