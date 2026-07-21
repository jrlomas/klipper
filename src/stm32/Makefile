# Additional STM32 build rules

# Setup the toolchain
CROSS_PREFIX=arm-none-eabi-

dirs-y += src/stm32 src/generic lib/fast-hash
dirs-$(CONFIG_MACH_STM32F0) += lib/stm32f0
dirs-$(CONFIG_MACH_STM32F1) += lib/stm32f1
dirs-$(CONFIG_MACH_N32G45x) += lib/n32g45x
dirs-$(CONFIG_MACH_STM32F2) += lib/stm32f2
dirs-$(CONFIG_MACH_STM32F4) += lib/stm32f4
dirs-$(CONFIG_MACH_STM32F7) += lib/stm32f7
dirs-$(CONFIG_MACH_STM32G0) += lib/stm32g0
dirs-$(CONFIG_MACH_STM32G4) += lib/stm32g4
dirs-$(CONFIG_MACH_STM32H7) += lib/stm32h7
dirs-$(CONFIG_MACH_STM32L4) += lib/stm32l4

MCU := $(shell echo $(CONFIG_MCU))
MCU_UPPER := $(shell echo $(CONFIG_MCU) | tr a-z A-Z | tr X x)

CFLAGS-$(CONFIG_HAVE_LIMITED_CODE_SIZE) += -Os
CFLAGS-$(CONFIG_MACH_STM32F0) += -mcpu=cortex-m0 -Ilib/stm32f0/include
CFLAGS-$(CONFIG_MACH_STM32F103) += -mcpu=cortex-m3
CFLAGS-$(CONFIG_MACH_N32G45x) += -mcpu=cortex-m4 -Ilib/n32g45x/include
CFLAGS-$(CONFIG_MACH_STM32F1) += -Ilib/stm32f1/include
CFLAGS-$(CONFIG_MACH_STM32F2) += -mcpu=cortex-m3 -Ilib/stm32f2/include
CFLAGS-$(CONFIG_MACH_STM32F4) += -mcpu=cortex-m4 -Ilib/stm32f4/include
CFLAGS-$(CONFIG_MACH_STM32F7) += -mcpu=cortex-m7 -Ilib/stm32f7/include
CFLAGS-$(CONFIG_MACH_STM32G0) += -mcpu=cortex-m0plus -Ilib/stm32g0/include
CFLAGS-$(CONFIG_MACH_STM32G4) += -mcpu=cortex-m4 -Ilib/stm32g4/include
CFLAGS-$(CONFIG_MACH_STM32H7) += -mcpu=cortex-m7 -Ilib/stm32h7/include
CFLAGS-$(CONFIG_MACH_STM32L4) += -mcpu=cortex-m4 -Ilib/stm32l4/include
CFLAGS += $(CFLAGS-y) -D$(MCU_UPPER) -mthumb -Ilib/cmsis-core -Ilib/fast-hash

CFLAGS_klipper.elf += -nostdlib -lgcc -lc_nano
CFLAGS_klipper.elf += -T $(OUT)src/generic/armcm_link.ld
$(OUT)klipper.elf: $(OUT)src/generic/armcm_link.ld

# Add source files
src-y += stm32/watchdog.c stm32/clockline.c stm32/dfu_reboot.c
src-y += generic/crc16_ccitt.c
src-y += generic/armcm_boot.c generic/armcm_irq.c generic/armcm_reset.c
src-$(CONFIG_MACH_STM32F0) += stm32/stm32f0.c ../lib/stm32f0/system_stm32f0xx.c
src-$(CONFIG_MACH_STM32F1) += stm32/stm32f1.c ../lib/stm32f1/system_stm32f1xx.c
src-$(CONFIG_MACH_STM32F2) += stm32/stm32f4.c ../lib/stm32f2/system_stm32f2xx.c
src-$(CONFIG_MACH_STM32F4) += stm32/stm32f4.c ../lib/stm32f4/system_stm32f4xx.c
src-$(CONFIG_MACH_STM32F7) += stm32/stm32f7.c ../lib/stm32f7/system_stm32f7xx.c
src-$(CONFIG_MACH_STM32G0) += stm32/stm32g0.c
src-$(CONFIG_MACH_STM32G4) += stm32/stm32g4.c ../lib/stm32g4/system_stm32g4xx.c
src-$(CONFIG_MACH_STM32H7) += stm32/stm32h7.c ../lib/stm32h7/system_stm32h7xx.c
src-$(CONFIG_MACH_STM32L4) += stm32/stm32l4.c ../lib/stm32l4/system_stm32l4xx.c
src-$(CONFIG_NEED_DMA_RESOURCE) += $(if $(filter y,$(CONFIG_MACH_STM32F7) \
	$(CONFIG_MACH_STM32H7)),stm32/dma_mpu.c)
timer-src-y := generic/armcm_timer.c
timer-src-$(CONFIG_MACH_STM32F0) := generic/timer_irq.c stm32/stm32f0_timer.c
timer-src-$(CONFIG_MACH_STM32G0) := generic/timer_irq.c stm32/stm32f0_timer.c
gpio-src-y := stm32/gpio.c stm32/gpioperiph.c
gpio-src-$(CONFIG_MACH_STM32F1) := stm32/gpio.c
gpio-src-$(CONFIG_MACH_STM32H7) := stm32/stm32h7_gpio.c stm32/gpioperiph.c
src-y += $(timer-src-y) $(gpio-src-y)
adc-src-y := stm32/adc.c
adc-src-$(CONFIG_MACH_STM32F0) := stm32/stm32f0_adc.c
adc-src-$(CONFIG_MACH_N32G45x) := ../lib/n32g45x/n32g45x_adc.c stm32/n32g45x_adc.c
adc-src-$(CONFIG_MACH_STM32G0) := stm32/stm32f0_adc.c
adc-src-$(CONFIG_MACH_STM32G4) := stm32/stm32h7_adc.c
adc-src-$(CONFIG_MACH_STM32H7) := stm32/stm32h7_adc.c
adc-src-$(CONFIG_MACH_STM32L4) := stm32/stm32h7_adc.c
src-$(CONFIG_WANT_ADC) += $(adc-src-y)
adc-stream-src-$(CONFIG_MACH_STM32F0) := stm32/stm32f0_adc_stream.c
adc-stream-src-$(CONFIG_MACH_STM32G0) := stm32/stm32f0_adc_stream.c
adc-stream-src-$(CONFIG_MACH_STM32F4) := stm32/stm32f4_adc_stream.c
adc-stream-src-$(CONFIG_MACH_STM32F7) := stm32/stm32f4_adc_stream.c
adc-stream-src-$(CONFIG_MACH_STM32H7) := stm32/stm32h7_adc_stream.c
src-$(CONFIG_WANT_ADC_STREAM) += $(adc-stream-src-y)
spi-src-y := stm32/spi.c
spi-src-$(CONFIG_MACH_STM32H7) := stm32/stm32h7_spi.c
src-$(CONFIG_WANT_SPI) += $(spi-src-y)
i2c-src-y := stm32/stm32f0_i2c.c
i2c-src-$(CONFIG_MACH_STM32F1) := stm32/i2c.c
i2c-src-$(CONFIG_MACH_STM32F2) := stm32/i2c.c
i2c-src-$(CONFIG_MACH_STM32F4) := stm32/i2c.c
src-$(CONFIG_WANT_I2C) += $(i2c-src-y)
serial-src-y := stm32/serial.c
serial-src-$(CONFIG_MACH_STM32F0) := stm32/stm32f0_serial.c
serial-src-$(CONFIG_MACH_STM32G0) := stm32/stm32f0_serial.c
serial-src-$(CONFIG_MACH_STM32G4) := stm32/stm32f0_serial.c
serial-src-$(CONFIG_MACH_STM32H7) := stm32/stm32f0_serial.c
src-$(CONFIG_SERIAL) += $(serial-src-y) generic/serial_irq.c
usb-src-$(CONFIG_HAVE_STM32_USBFS) := stm32/usbfs.c
usb-src-$(CONFIG_HAVE_STM32_USBOTG) := stm32/usbotg.c
usb-sof-src-$(CONFIG_HAVE_STM32_USBFS) := generic/usb_sof.c
src-$(CONFIG_USBSERIAL) += $(usb-src-y) stm32/chipid.c generic/usb_cdc.c \
    $(usb-sof-src-y)
canhw-src-$(CONFIG_HAVE_STM32_CANBUS) += stm32/can.c
canhw-src-$(CONFIG_HAVE_STM32_FDCANBUS) += stm32/fdcan.c
canserial-src-y := generic/canserial.c ../lib/fast-hash/fasthash.c
src-$(CONFIG_CANSERIAL) += $(canserial-src-y) $(canhw-src-y) \
    generic/canbus.c stm32/chipid.c
usbcan-console-src-y := generic/canserial.c ../lib/fast-hash/fasthash.c
usbcan-console-src-$(CONFIG_HELIX_USB_CAN_COMPOSITE) := \
    generic/usb_local_console.c
src-$(CONFIG_USBCANBUS) += $(usb-src-y) $(canhw-src-y) $(usbcan-console-src-y)
src-$(CONFIG_USBCANBUS) += stm32/chipid.c generic/usb_canbus.c \
    generic/can_gateway.c $(usb-sof-src-y)
src-$(CONFIG_GATEWAY_RMII) += $(canhw-src-y)
src-$(CONFIG_WANT_HARD_PWM) += stm32/hard_pwm.c
src-$(CONFIG_HAVE_GPIO_SDIO) += stm32/sdio.c
src-$(CONFIG_MACH_STM32G0) += stm32/comp.c
src-$(CONFIG_WANT_TRIGGER_SOURCE) += stm32/gpio_exti.c
src-$(CONFIG_WANT_TRIGGER_SOURCE) += stm32/adc_watchdog.c stm32/timer_capture.c

# Native RMII Ethernet MAC/DMA datagram console.  STM32H7 uses the newer
# Synopsys descriptor/register layout and must not share the F4/F7 backend.
eth-mac-src-y := stm32/eth_mac.c
eth-mac-src-$(CONFIG_MACH_STM32H7) := stm32/eth_mac_h7.c
src-$(CONFIG_WANT_ETHERNET_RMII) += $(eth-mac-src-y)

# Ethernet datagram console: the intentproto datagram codec is
# freestanding C++ (no exceptions/rtti/heap); compile it with the ARM
# g++ and link it alongside the LTO C objects, exactly as the linux and
# esp32 ports do for the same udp_console.c glue.
# The intentproto C++ codec is pulled in by the datagram console (Ethernet)
# and/or the serial console framing v2 (BCH). Both need datagram.o + bch.o +
# hmac.o; udp_datagram.o is Ethernet-only, framing_v2.o is console-v2-only.
ifneq ($(filter y,$(CONFIG_NEED_UDP_CONSOLE) $(CONFIG_NEED_UDP_GATEWAY) $(CONFIG_WANT_CONSOLE_FRAMING_V2)),)
CXX = $(CROSS_PREFIX)g++
w5500-cxxflags := $(CFLAGS-y) -mthumb -D$(MCU_UPPER) \
	-iquote $(OUT) -iquote src/generic -Ilib/intentproto/include \
	-std=c++17 -Os -MD -Wall -fno-exceptions -fno-rtti \
	-fno-threadsafe-statics -ffunction-sections -fdata-sections -ggdb3
OBJS_klipper.elf += $(OUT)lib/intentproto/src/datagram.o \
	$(OUT)lib/intentproto/src/bch.o \
	$(OUT)lib/intentproto/src/hmac.o
ifeq ($(CONFIG_WANT_DATAGRAM_SESSION),y)
OBJS_klipper.elf += $(OUT)lib/intentproto/src/session_sec.o \
	$(OUT)lib/intentproto/src/sha512.o
endif
ifneq ($(filter y,$(CONFIG_NEED_UDP_CONSOLE) $(CONFIG_NEED_UDP_GATEWAY)),)
OBJS_klipper.elf += $(OUT)src/generic/udp_datagram.o
$(OUT)src/generic/udp_datagram.o: src/generic/udp_datagram.cpp $(OUT)autoconf.h
	@echo "  Compiling (C++) $@"
	$(Q)$(CXX) $(w5500-cxxflags) -c $< -o $@
endif
ifeq ($(CONFIG_WANT_CONSOLE_FRAMING_V2),y)
OBJS_klipper.elf += $(OUT)src/generic/framing_v2.o
$(OUT)src/generic/framing_v2.o: src/generic/framing_v2.cpp $(OUT)autoconf.h
	@echo "  Compiling (C++) $@"
	$(Q)$(CXX) $(w5500-cxxflags) -c $< -o $@
endif

$(OUT)lib/intentproto/src/%.o: lib/intentproto/src/%.cpp $(OUT)autoconf.h
	@echo "  Compiling (C++) $@"
	$(Q)mkdir -p $(OUT)lib/intentproto/src
	$(Q)$(CXX) $(w5500-cxxflags) -c $< -o $@
endif

# Binary output file rules
target-y += $(OUT)klipper.bin

$(OUT)klipper.bin: $(OUT)klipper.elf
	@echo "  Creating bin file $@"
	$(Q)$(OBJCOPY) -O binary $< $@

# Flash rules
lib/hidflash/hid-flash:
	@echo "  Building hid-flash"
	$(Q)make -C lib/hidflash

flash: $(OUT)klipper.bin lib/hidflash/hid-flash
	@echo "  Flashing $< to $(FLASH_DEVICE)"
	$(Q)$(PYTHON) ./scripts/flash_usb.py -t $(CONFIG_MCU) -d "$(FLASH_DEVICE)" -s "$(CONFIG_FLASH_APPLICATION_ADDRESS)" $(if $(NOSUDO),--no-sudo) $(OUT)klipper.bin

serialflash: $(OUT)klipper.bin
	@echo "  Flashing $< to $(FLASH_DEVICE) via stm32flash"
	$(Q)stm32flash -w $< -v -g 0 $(FLASH_DEVICE)
