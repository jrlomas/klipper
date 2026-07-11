# intentproto standalone build: proves the library compiles with
# nothing but a C++ compiler, and runs the desktop test suite.
#
#   make test        - build and run tests with the host compiler
#   make embedded    - size-check compile with arm-none-eabi-g++ (if present)

CXX ?= g++
CXXFLAGS ?= -std=gnu++17 -Os -g -Wall -Wextra -fno-exceptions -fno-rtti
CPPFLAGS += -Iinclude

BUILD := build
SRCS := src/proto.cpp src/dict.cpp

.PHONY: all test embedded clean

all: test

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/test_proto: $(SRCS) tests/test_proto.cpp \
		include/intentproto/proto.hpp include/intentproto/method.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) $(CPPFLAGS) $(SRCS) tests/test_proto.cpp -o $@

test: $(BUILD)/test_proto
	./$(BUILD)/test_proto

# Cortex-M0 (STM32F072 floor target) compile + size report; skipped
# politely when the cross compiler is not installed.
embedded: | $(BUILD)
	@if command -v arm-none-eabi-g++ >/dev/null 2>&1; then \
		arm-none-eabi-g++ $(CXXFLAGS) $(CPPFLAGS) -mcpu=cortex-m0 -mthumb \
			-ffunction-sections -fdata-sections -c src/proto.cpp \
			-o $(BUILD)/proto.o && \
		arm-none-eabi-g++ $(CXXFLAGS) $(CPPFLAGS) -mcpu=cortex-m0 -mthumb \
			-ffunction-sections -fdata-sections -c src/dict.cpp \
			-o $(BUILD)/dict.o && \
		arm-none-eabi-size $(BUILD)/proto.o $(BUILD)/dict.o; \
	else \
		echo "arm-none-eabi-g++ not found; skipping embedded build"; \
	fi

clean:
	rm -rf $(BUILD)
