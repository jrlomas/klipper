# Additional linux build rules

dirs-y += src/linux src/generic

src-y += linux/main.c linux/timer.c linux/console.c linux/watchdog.c
src-y += linux/pca9685.c linux/spidev.c linux/analog.c linux/hard_pwm.c
src-y += linux/i2c.c linux/gpio.c generic/crc16_ccitt.c generic/alloc.c
src-y += linux/sensor_ds18b20.c

# Datagram (UDP) console transport (RFC 0001 doc 07)
src-y += linux/udp.c generic/udp_console.c

# The intentproto datagram link layer is freestanding C++ (no
# exceptions/rtti/heap); it is compiled separately and linked in
CXX = $(CROSS_PREFIX)g++
udpdg-objs := $(OUT)src/generic/udp_datagram.o \
	$(OUT)lib/intentproto/src/datagram.o \
	$(OUT)lib/intentproto/src/bch.o \
	$(OUT)lib/intentproto/src/hmac.o
OBJS_klipper.elf += $(udpdg-objs)
CXXFLAGS_udpdg := -iquote src/generic -Ilib/intentproto/include -std=c++17 \
	-O2 -MD -Wall -fno-exceptions -fno-rtti -ffunction-sections \
	-fdata-sections -ggdb3

$(OUT)src/generic/udp_datagram.o: src/generic/udp_datagram.cpp
	@echo "  Compiling (C++) $@"
	$(Q)$(CXX) $(CXXFLAGS_udpdg) -c $< -o $@

$(OUT)lib/intentproto/src/%.o: lib/intentproto/src/%.cpp
	@echo "  Compiling (C++) $@"
	$(Q)mkdir -p $(OUT)lib/intentproto/src
	$(Q)$(CXX) $(CXXFLAGS_udpdg) -c $< -o $@

CFLAGS_klipper.elf += -lutil -lrt -lpthread

flash: $(OUT)klipper.elf
	@echo "  Flashing"
	$(Q)sudo ./scripts/flash-linux.sh $(OUT)
