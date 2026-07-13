# intentproto standalone build: proves the library compiles with
# nothing but a C++ compiler, and runs the desktop test suite.
#
#   make test        - build and run tests with the host compiler
#   make capi        - build the host-profile C ABI + cffi binding and
#                      run the C and Python round-trip tests (doc 10)
#   make embedded    - size-check compile with arm-none-eabi-g++ (if present)
#   make dict        - demonstrate the identify-blob build (tools/mkdict.py)

CXX ?= g++
CC ?= gcc
CXXFLAGS ?= -std=gnu++17 -Os -g -Wall -Wextra -fno-exceptions -fno-rtti
CFLAGS ?= -std=gnu11 -Os -g -Wall -Wextra
CPPFLAGS += -Iinclude

BUILD := build
# src/capi.cpp is the host-profile extern "C" shim (doc 10); it carries
# no static registration, so linking it into every test is harmless.
SRCS := src/proto.cpp src/dict.cpp src/host.cpp src/hmac.cpp src/bch.cpp \
	src/datagram.cpp src/datagram_carrier.cpp src/session_sec.cpp \
	src/can_transport.cpp \
	src/sha512.cpp src/ed25519.cpp src/segment.cpp \
	src/capi.cpp boot/bootcore.cpp
HDRS := $(wildcard include/intentproto/*.hpp) include/intentproto/capi.h
TESTS := test_proto test_host test_hmac test_bch test_datagram \
	test_datagram_carrier \
	test_negotiate test_extdesc test_wide test_bootcore test_session_sec \
	test_can_transport test_sha512 test_ed25519 test_segment

.PHONY: all test capi embedded dict clean bootsim

# Desktop bootloader simulator for end-to-end flasher tests (needs zlib).
bootsim: $(BUILD)/bootsim
$(BUILD)/bootsim: tools/bootsim.cpp $(SRCS) $(HDRS) | $(BUILD)
	$(CXX) $(CXXFLAGS) $(CPPFLAGS) src/proto.cpp src/dict.cpp \
		src/datagram.cpp src/bch.cpp src/hmac.cpp src/sha512.cpp \
		src/ed25519.cpp boot/bootcore.cpp \
		tools/bootsim.cpp -lz -o $@

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
		echo "== test_ed25519_e2e (python signer <-> C verifier)"; \
		python3 tools/test_ed25519_e2e.py || exit 1; \
	else \
		echo "== test_extbind (skipped: no python3)"; \
		echo "== test_ed25519_e2e (skipped: no python3)"; \
	fi

# Host-profile C ABI (doc 10). test_capi.c is compiled as C to prove
# capi.h is valid C and the ABI links from a C translation unit; it
# then links the C++ core. The cffi API-mode binding (python/) builds
# the same sources into a Python extension and drives the same
# host-session loopback; both are skipped politely when their
# toolchains are absent.
$(BUILD)/test_capi: tests/test_capi.c $(SRCS) $(HDRS) | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) -c tests/test_capi.c -o $(BUILD)/test_capi_c.o
	$(CXX) $(CXXFLAGS) $(CPPFLAGS) $(SRCS) $(BUILD)/test_capi_c.o -o $@

capi: $(BUILD)/test_capi
	@echo "== test_capi (C ABI loopback)"; ./$(BUILD)/test_capi || exit 1
	@if command -v python3 >/dev/null 2>&1 \
			&& python3 -c "import cffi" >/dev/null 2>&1; then \
		echo "== test_capi_py (cffi API mode)"; \
		python3 python/test_capi_py.py || exit 1; \
		echo "== test_extbind_py (packaged extension binding)"; \
		python3 python/test_extbind_py.py || exit 1; \
	else \
		echo "== test_capi_py (skipped: no python3/cffi)"; \
		echo "== test_extbind_py (skipped: no python3/cffi)"; \
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
