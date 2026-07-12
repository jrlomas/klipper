# First-class bootloader objectset — compile-check build (RFC 0001
# doc 11).
#
# This is the STAGED objectset, not yet wired into the default
# firmware build (see README.md, "Build status"). `make check` cross-
# compiles every translation unit for all three arm targets to confirm
# the bootloader, the per-target flash drivers, and the linker-script
# symbols compile against the vendored CMSIS headers. A full ELF link
# additionally needs the port's startup (armcm_boot.c) and transport
# glue; the assembly recipe is documented in README.md.

REPO := $(abspath ../..)
CROSS ?= arm-none-eabi-
CXX := $(CROSS)g++
CC := $(CROSS)gcc

IP := $(REPO)/lib/intentproto
COMMON := -I$(REPO)/lib/cmsis-core -I$(IP)/include -I. \
	-ffreestanding -ffunction-sections \
	-fdata-sections -Os -Wall -Wextra -mthumb
CXXFLAGS := -std=c++17 -fno-exceptions -fno-rtti $(COMMON)
CFLAGS := -std=gnu11 $(COMMON)

BUILD := build

# Portable / protocol translation units (target-independent).
PORTABLE_CPP := boot_main.cpp $(IP)/boot/bootcore.cpp
PORTABLE_C := boot_flash.c

# Per-target: cpu flags, device include dir, driver source.
f072_CPU := -mcpu=cortex-m0
f072_INC := -I$(REPO)/lib/stm32f0/include
f072_DRV := boot_flash_stm32f072.c

g0b1_CPU := -mcpu=cortex-m0plus
g0b1_INC := -I$(REPO)/lib/stm32g0/include
g0b1_DRV := boot_flash_stm32g0b1.c

f4_CPU := -mcpu=cortex-m4
f4_INC := -I$(REPO)/lib/stm32f4/include
f4_DRV := boot_flash_stm32f4.c

TARGETS := f072 g0b1 f4

.PHONY: check test clean

# Host build of the portable FlashOps logic tests (part 4). No cross
# compiler or registers involved — pure geometry against a fake flash.
HOSTCXX ?= g++
test:
	@mkdir -p $(BUILD)
	$(HOSTCXX) -std=c++17 -Os -Wall -Wextra -fno-exceptions -fno-rtti \
		-I$(IP)/include -I. \
		test_flashops.cpp $(IP)/boot/bootcore.cpp boot_flash.c \
		-o $(BUILD)/test_flashops
	./$(BUILD)/test_flashops

check:
	@mkdir -p $(BUILD)
	@for t in $(TARGETS); do \
	  cpu=$$(eval echo \$${$${t}_CPU}); inc=$$(eval echo \$${$${t}_INC}); \
	  drv=$$(eval echo \$${$${t}_DRV}); \
	  echo "== $$t =="; \
	  for f in $(PORTABLE_CPP); do \
	    $(CXX) $(CXXFLAGS) $$cpu $$inc -c $$f \
	      -o $(BUILD)/$$t-$$(basename $$f .cpp).o || exit 1; done; \
	  for f in $(PORTABLE_C) $$drv; do \
	    $(CC) $(CFLAGS) $$cpu $$inc -c $$f \
	      -o $(BUILD)/$$t-$$(basename $$f .c).o || exit 1; done; \
	  $(CROSS)size $(BUILD)/$$t-*.o | tail -n +2 | \
	    awk '{t+=$$1+$$2} END{printf "   %s objectset .text+.data = %d bytes\n","'$$t'",t}'; \
	done
	@echo "all targets compiled"

clean:
	rm -rf $(BUILD)
