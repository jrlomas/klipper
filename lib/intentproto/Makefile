# intentproto standalone build: proves the library compiles with
# nothing but a C++ compiler, and runs the desktop test suite.
#
#   make test        - build and run tests with the host compiler
#   make embedded    - size-check compile with arm-none-eabi-g++ (if present)
#   make dict        - demonstrate the identify-blob build (tools/mkdict.py)

CXX ?= g++
CXXFLAGS ?= -std=gnu++17 -Os -g -Wall -Wextra -fno-exceptions -fno-rtti
CPPFLAGS += -Iinclude

BUILD := build
SRCS := src/proto.cpp src/dict.cpp src/host.cpp src/hmac.cpp src/bch.cpp \
	src/datagram.cpp boot/bootcore.cpp
HDRS := $(wildcard include/intentproto/*.hpp)
TESTS := test_proto test_host test_hmac test_bch test_datagram \
	test_negotiate test_extdesc test_bootcore

.PHONY: all test embedded dict clean

all: test

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/test_%: tests/test_%.cpp $(SRCS) $(HDRS) | $(BUILD)
	$(CXX) $(CXXFLAGS) $(CPPFLAGS) $(SRCS) $< -o $@

# test_extdesc must run from the package root so it writes
# build/extdesc_wire.bin, which the python binding test then reads.
test: $(addprefix $(BUILD)/,$(TESTS))
	@for t in $(TESTS); do echo "== $$t"; ./$(BUILD)/$$t || exit 1; done
	@if command -v python3 >/dev/null 2>&1; then \
		echo "== test_extbind"; python3 tools/test_extbind.py || exit 1; \
	else \
		echo "== test_extbind (skipped: no python3)"; \
	fi

# Cortex-M0 (STM32F072 floor target) compile + size report; skipped
# politely when the cross compiler is not installed.
embedded: | $(BUILD)
	@if command -v arm-none-eabi-g++ >/dev/null 2>&1; then \
		for f in $(SRCS); do \
			arm-none-eabi-g++ $(CXXFLAGS) $(CPPFLAGS) -mcpu=cortex-m0 -mthumb \
				-ffunction-sections -fdata-sections -c $$f \
				-o $(BUILD)/$$(basename $$f .cpp).o || exit 1; \
		done && arm-none-eabi-size $(BUILD)/*.o | grep -v test; \
	else \
		echo "arm-none-eabi-g++ not found; skipping embedded build"; \
	fi

# Identify-blob build demo: firmware projects compile
# tools/dump_dict.cpp together with THEIR declaration translation
# units; here it is built with its own example declarations.
dict: | $(BUILD)
	$(CXX) $(CXXFLAGS) $(CPPFLAGS) $(SRCS) tools/dump_dict.cpp \
		-o $(BUILD)/dump_dict
	python3 tools/mkdict.py $(BUILD)/dump_dict -o $(BUILD) \
		--version intentproto-dev --build-version standalone

clean:
	rm -rf $(BUILD)
