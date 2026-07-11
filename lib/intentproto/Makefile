# intentproto standalone build: proves the library compiles with
# nothing but a C++ compiler, and runs the desktop test suite.
#
#   make test        - build and run tests with the host compiler
#   make embedded    - size-check compile with arm-none-eabi-g++ (if present)

CXX ?= g++
CXXFLAGS ?= -std=gnu++17 -Os -g -Wall -Wextra -fno-exceptions -fno-rtti
CPPFLAGS += -Iinclude

BUILD := build
SRCS := src/proto.cpp src/dict.cpp src/hmac.cpp src/bch.cpp src/datagram.cpp
HDRS := $(wildcard include/intentproto/*.hpp)
TESTS := test_proto test_hmac test_bch test_datagram

.PHONY: all test embedded clean

all: test

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/test_%: tests/test_%.cpp $(SRCS) $(HDRS) | $(BUILD)
	$(CXX) $(CXXFLAGS) $(CPPFLAGS) $(SRCS) $< -o $@

test: $(addprefix $(BUILD)/,$(TESTS))
	@for t in $(TESTS); do echo "== $$t"; ./$(BUILD)/$$t || exit 1; done

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

clean:
	rm -rf $(BUILD)
