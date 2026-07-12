# First-class bootloader build (RFC 0001 doc 11).
#
# Targets:
#   make check      cross-compile every translation unit for all three
#                   arm targets (fast compile sanity, no link).
#   make test       host build + run of the portable FlashOps tests.
#   make bootloader link real, flashable bootloader ELF+BIN images for
#                   STM32F072 and STM32F4 and report size vs budget.
#   make combined   assemble a bootloader.bin + app.bin into ONE
#                   flashable image (needs BOOT_BIN, APP_BIN, TARGET).
#
# The bootloader image is the object set here + the minimal startup
# (boot_startup.c) + the polled UART transport (boot_uart_*.c) + the
# intentproto protocol library, linked with boot_stm32<target>.ld which
# pins the vector table at flash base and the app region behind it.

REPO := $(abspath ../..)
CROSS ?= arm-none-eabi-
CXX := $(CROSS)g++
CC := $(CROSS)gcc
OBJCOPY := $(CROSS)objcopy
SIZE := $(CROSS)size

IP := $(REPO)/lib/intentproto
COMMON := -I$(REPO)/lib/cmsis-core -I$(IP)/include -I. \
	-ffreestanding -ffunction-sections \
	-fdata-sections -Os -Wall -Wextra -mthumb
CXXFLAGS := -std=c++17 -fno-exceptions -fno-rtti -fno-threadsafe-statics \
	-fno-use-cxa-atexit $(COMMON)
CFLAGS := -std=gnu11 $(COMMON)

BUILD := build

# Portable / protocol translation units (target-independent).
PORTABLE_CPP := boot_main.cpp $(IP)/boot/bootcore.cpp
PORTABLE_C := boot_flash.c

# Full intentproto protocol library the bootloader links to speak the
# same framing/dictionary/Class-1 surface as the application. proto +
# dict + datagram(v2 framing) + bch + hmac; --gc-sections drops the
# network-only paths a UART bootloader does not reach.
IP_LIB_CPP := $(IP)/src/proto.cpp $(IP)/src/dict.cpp \
	$(IP)/src/datagram.cpp $(IP)/src/bch.cpp $(IP)/src/hmac.cpp

# Bootloader image translation units (startup + transport, per target).
LINK_CPP := $(PORTABLE_CPP) $(IP_LIB_CPP)
LINK_C := $(PORTABLE_C) boot_startup.c

.PHONY: check test bootloader combined compile-one link-one clean

# ---- portable FlashOps logic tests (part 4) ----
# No cross compiler or registers — pure geometry against a fake flash.
HOSTCXX ?= g++
test:
	@mkdir -p $(BUILD)
	$(HOSTCXX) -std=c++17 -Os -Wall -Wextra -fno-exceptions -fno-rtti \
		-I$(IP)/include -I. \
		test_flashops.cpp $(IP)/boot/bootcore.cpp boot_flash.c \
		-o $(BUILD)/test_flashops
	./$(BUILD)/test_flashops

# ---- compile-check every TU for every target ----
# Per-target flags are passed as real make variables (recursive make),
# not shell indirection, so the device driver is actually built.
check:
	@$(MAKE) --no-print-directory compile-one T=f072 \
	  CPU="-mcpu=cortex-m0" INC="-I$(REPO)/lib/stm32f0/include" \
	  DRV=boot_flash_stm32f072.c DEV=stm32f072xb.h
	@$(MAKE) --no-print-directory compile-one T=g0b1 \
	  CPU="-mcpu=cortex-m0plus" INC="-I$(REPO)/lib/stm32g0/include" \
	  DRV=boot_flash_stm32g0b1.c DEV=stm32g0b1xx.h
	@$(MAKE) --no-print-directory compile-one T=f4 \
	  CPU="-mcpu=cortex-m4" INC="-I$(REPO)/lib/stm32f4/include" \
	  DRV=boot_flash_stm32f4.c DEV=stm32f407xx.h
	@echo "all targets compiled"

compile-one:
	@mkdir -p $(BUILD)
	@echo "== $(T) =="
	@for f in $(PORTABLE_CPP); do \
	  $(CXX) $(CXXFLAGS) $(CPU) $(INC) -c $$f \
	    -o $(BUILD)/$(T)-$$(basename $$f .cpp).o || exit 1; done
	@for f in $(PORTABLE_C) $(DRV); do \
	  $(CC) $(CFLAGS) $(CPU) $(INC) -DBOOT_DEVICE_HEADER='"$(DEV)"' -c $$f \
	    -o $(BUILD)/$(T)-$$(basename $$f .c).o || exit 1; done
	@$(SIZE) $(BUILD)/$(T)-*.o | tail -n +2 | \
	  awk '{t+=$$1+$$2} END{printf "   %s objectset .text+.data = %d bytes\n","$(T)",t}'

# ---- link the real, flashable bootloader images ----
bootloader:
	@$(MAKE) --no-print-directory link-one T=f072 \
	  CPU="-mcpu=cortex-m0" INC="-I$(REPO)/lib/stm32f0/include" \
	  DRV=boot_flash_stm32f072.c UART=boot_uart_stm32f0.c \
	  DEV=stm32f072xb.h LD=boot_stm32f072.ld BUDGET=16384
	@$(MAKE) --no-print-directory link-one T=f4 \
	  CPU="-mcpu=cortex-m4" INC="-I$(REPO)/lib/stm32f4/include" \
	  DRV=boot_flash_stm32f4.c UART=boot_uart_stm32f4.c \
	  DEV=stm32f407xx.h LD=boot_stm32f4.ld BUDGET=32768
	@echo "bootloader images linked"

link-one:
	@mkdir -p $(BUILD)
	@echo "== bootloader $(T) =="
	@objs=""; \
	for f in $(LINK_CPP); do o=$(BUILD)/$(T)-$$(basename $$f .cpp).o; \
	  $(CXX) $(CXXFLAGS) $(CPU) $(INC) -c $$f -o $$o || exit 1; \
	  objs="$$objs $$o"; done; \
	for f in $(LINK_C) $(DRV) $(UART); do o=$(BUILD)/$(T)-$$(basename $$f .c).o; \
	  $(CC) $(CFLAGS) $(CPU) $(INC) -DBOOT_DEVICE_HEADER='"$(DEV)"' \
	    -c $$f -o $$o || exit 1; objs="$$objs $$o"; done; \
	$(CXX) $(CPU) -mthumb -nostdlib -T $(LD) -Wl,--gc-sections \
	  -Wl,-e,ResetHandler $$objs -lgcc -lc_nano \
	  -o $(BUILD)/boot-$(T).elf || exit 1; \
	$(OBJCOPY) -O binary $(BUILD)/boot-$(T).elf $(BUILD)/boot-$(T).bin; \
	sz=$$(stat -c%s $(BUILD)/boot-$(T).bin); \
	printf "   boot-%s.bin = %d bytes (budget %d)\n" "$(T)" "$$sz" "$(BUDGET)"; \
	[ $$sz -le $(BUDGET) ] || { echo "   OVER BUDGET"; exit 1; }

# ---- assemble a combined one-flash image ----
# Example:
#   make combined TARGET=stm32f072 \
#        BOOT_BIN=build/boot-f072.bin APP_BIN=../../out/klipper.bin \
#        OUT=build/combined-f072.bin
combined:
	$(REPO)/scripts/build_combined.py $(TARGET) $(BOOT_BIN) $(APP_BIN) -o $(OUT)

clean:
	rm -rf $(BUILD)
