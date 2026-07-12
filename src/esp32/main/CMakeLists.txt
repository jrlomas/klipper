# klipper firmware as an ESP-IDF component
#
# Layout mirrors klipper's own build:
#  * klipper C sources (which carry .compile_time_request sections -
#    see src/ctr.h) are compiled into a CMake OBJECT library
#  * gen_compile_time_request.py objcopy-extracts those sections and
#    runs scripts/buildcommands.py, producing klipper.dict and the
#    generated compile_time_request.c
#  * this component compiles the generated file plus the C++
#    intentproto link layer and links the object library in
#
# Include resolution matches the Makefile flow: a "board" symlink to
# src/esp32 and a board-generic/board symlink to src/generic, with a
# hand-written static autoconf.h in src/esp32 (idf.py replaces
# klipper's Kconfig for this target).

set(KLIPPER_ROOT ${CMAKE_CURRENT_LIST_DIR}/../../..)
get_filename_component(KLIPPER_ROOT ${KLIPPER_ROOT} ABSOLUTE)

# --- klipper include layout (board/ -> src/esp32, generic fallback) ---
set(KLIPPER_INC_ROOT ${CMAKE_BINARY_DIR}/klipper_inc)
file(MAKE_DIRECTORY ${KLIPPER_INC_ROOT})
file(MAKE_DIRECTORY ${KLIPPER_INC_ROOT}/board-generic)
file(CREATE_LINK ${KLIPPER_ROOT}/src/esp32
     ${KLIPPER_INC_ROOT}/board SYMBOLIC)
file(CREATE_LINK ${KLIPPER_ROOT}/src/generic
     ${KLIPPER_INC_ROOT}/board-generic/board SYMBOLIC)
set(KLIPPER_INCLUDES
    ${KLIPPER_INC_ROOT}
    ${KLIPPER_ROOT}/src/esp32
    ${KLIPPER_ROOT}/src
    ${KLIPPER_INC_ROOT}/board-generic
    # Vendored Apache-2.0 register headers (lib/esp32/README) come
    # before the IDF include dirs so the port's soc/*.h references
    # resolve to the in-tree copies, mirroring lib/stm32's CMSIS
    ${KLIPPER_ROOT}/lib/esp32/include
    ${KLIPPER_ROOT}/lib/intentproto/include)

# --- sources carrying compile-time requests ---
# The step backend is a build-time substitution (FD-0001 doc 12): with
# CONFIG_KLIPPER_RMT_STEP the esp32-specific src/esp32/rmt_stepper.c
# (added to the board sources below) provides the config_stepper/
# queue_step command surface driving the RMT peripheral, and the
# portable src/stepper.c is left out of the build; otherwise the
# classic timer-IRQ stepper.c is compiled as on every other target.
if(CONFIG_KLIPPER_RMT_STEP)
    set(KLIPPER_STEPPER_SRC "")
else()
    set(KLIPPER_STEPPER_SRC stepper.c)
endif()
set(KLIPPER_CORE_SRCS
    # core (see src/Makefile src-y)
    sched.c command.c basecmd.c debugcmds.c initial_pins.c
    gpiocmds.c ${KLIPPER_STEPPER_SRC} endstop.c trsync.c adccmds.c
    # peripheral command modules (CONFIG_WANT_* in autoconf.h)
    spicmds.c spi_software.c i2ccmds.c i2c_software.c pwmcmds.c
    buttons.c tmcuart.c neopixel.c
    # trajectory intention queue (FD-0001)
    trajq.c traj_stepper.c execlog.c timesync.c heater_hold.c)
list(TRANSFORM KLIPPER_CORE_SRCS PREPEND ${KLIPPER_ROOT}/src/)
set(KLIPPER_GENERIC_SRCS
    ${KLIPPER_ROOT}/src/generic/crc16_ccitt.c
    ${KLIPPER_ROOT}/src/generic/alloc.c
    ${KLIPPER_ROOT}/src/generic/timer_irq.c
    ${KLIPPER_ROOT}/src/generic/udp_console.c)
set(KLIPPER_BOARD_SRCS
    ${KLIPPER_ROOT}/src/esp32/main.c
    ${KLIPPER_ROOT}/src/esp32/gpio.c
    ${KLIPPER_ROOT}/src/esp32/adc.c
    ${KLIPPER_ROOT}/src/esp32/spi.c
    ${KLIPPER_ROOT}/src/esp32/i2c.c
    ${KLIPPER_ROOT}/src/esp32/hard_pwm.c
    ${KLIPPER_ROOT}/src/esp32/wifi.c
    # In-band update mapped onto ESP-IDF OTA (FD-0001 doc 11)
    ${KLIPPER_ROOT}/src/esp32/ota.c)
if(CONFIG_KLIPPER_ARCH_MODEM)
    # "IDF as modem" (FD-0001 doc 12 stage 3): bare core 1 booted by
    # appcpu_boot.c, console over the shared-memory ring, IDF/lwIP
    # confined to core 0.  rmt_step.c is component-arch only (it
    # needs esp_intr_alloc; revisit with the bare-core ISR work).
    list(APPEND KLIPPER_BOARD_SRCS
        ${KLIPPER_ROOT}/src/esp32/appcpu_boot.c
        ${KLIPPER_ROOT}/src/esp32/appcpu_vectors.S
        ${KLIPPER_ROOT}/src/esp32/shmem_console.c
        ${KLIPPER_ROOT}/src/esp32/modem.c)
else()
    # Component arch: klipper as a FreeRTOS task pinned to core 1
    list(APPEND KLIPPER_BOARD_SRCS
        ${KLIPPER_ROOT}/src/esp32/irq.c
        ${KLIPPER_ROOT}/src/esp32/timer.c
        ${KLIPPER_ROOT}/src/esp32/rmt_step.c
        ${KLIPPER_ROOT}/src/esp32/udp_port.c)
    if(CONFIG_KLIPPER_RMT_STEP)
        # RMT-backed stepper backend replacing the portable stepper.c
        list(APPEND KLIPPER_BOARD_SRCS
            ${KLIPPER_ROOT}/src/esp32/rmt_stepper.c)
    endif()
endif()

set(GEN_CTR_C ${CMAKE_CURRENT_BINARY_DIR}/compile_time_request.c)
set(GEN_DICT ${CMAKE_CURRENT_BINARY_DIR}/klipper.dict)

# --- the component itself: generated tables + C++ link layer ---
idf_component_register(
    SRCS
        ${GEN_CTR_C}
        ${KLIPPER_ROOT}/src/generic/udp_datagram.cpp
        ${KLIPPER_ROOT}/lib/intentproto/src/datagram.cpp
        ${KLIPPER_ROOT}/lib/intentproto/src/bch.cpp
        ${KLIPPER_ROOT}/lib/intentproto/src/hmac.cpp
    PRIV_INCLUDE_DIRS ${KLIPPER_INCLUDES}
    PRIV_REQUIRES driver esp_adc esp_wifi nvs_flash lwip esp_netif
        esp_event esp_timer app_update spi_flash
    LDFRAGMENTS linker.lf)
set_source_files_properties(${GEN_CTR_C} PROPERTIES GENERATED TRUE)

# --- object library with the klipper sources ---
add_library(klipper_core OBJECT
    ${KLIPPER_CORE_SRCS} ${KLIPPER_GENERIC_SRCS} ${KLIPPER_BOARD_SRCS})
target_include_directories(klipper_core PRIVATE ${KLIPPER_INCLUDES})
# Inherit the IDF build environment (sdkconfig dir, arch flags, ...)
idf_build_get_property(idf_includes INCLUDE_DIRECTORIES)
idf_build_get_property(idf_defines COMPILE_DEFINITIONS)
idf_build_get_property(idf_options COMPILE_OPTIONS)
idf_build_get_property(idf_c_options C_COMPILE_OPTIONS)
target_include_directories(klipper_core PRIVATE ${idf_includes})
target_compile_definitions(klipper_core PRIVATE ${idf_defines})
target_compile_options(klipper_core PRIVATE
    ${idf_options} ${idf_c_options}
    # klipper sources trip a few warnings under IDF's default set
    -Wno-error -Wno-unused-but-set-variable)
# Headers of the IDF components the board bindings use
foreach(comp freertos esp_common esp_system esp_hw_support esp_rom
        hal soc log heap esp_driver_gpio esp_driver_gptimer esp_adc
        esp_driver_ledc
        esp_wifi nvs_flash lwip esp_netif esp_event newlib esp_timer
        app_update spi_flash)
    if(TARGET __idf_${comp})
        target_link_libraries(klipper_core PRIVATE __idf_${comp})
    endif()
endforeach()

# --- dictionary + compile_time_request generation ---
idf_build_get_property(python PYTHON)
set(CTR_OBJLIST ${CMAKE_CURRENT_BINARY_DIR}/klipper_ctr_objs.txt)
file(GENERATE OUTPUT ${CTR_OBJLIST}
     CONTENT "$<TARGET_OBJECTS:klipper_core>")
add_custom_command(
    OUTPUT ${GEN_CTR_C} ${GEN_DICT}
    COMMAND ${python} ${KLIPPER_ROOT}/src/esp32/gen_compile_time_request.py
        --objcopy ${CMAKE_OBJCOPY}
        --objlist ${CTR_OBJLIST}
        --out-c ${GEN_CTR_C}
        --dict ${GEN_DICT}
        --klipper-root ${KLIPPER_ROOT}
        --tools "${CMAKE_C_COMPILER}\;${CMAKE_C_COMPILER}\;${CMAKE_C_COMPILER}\;${CMAKE_OBJCOPY}\;${CMAKE_OBJCOPY}\;${CMAKE_OBJCOPY}"
    DEPENDS klipper_core $<TARGET_OBJECTS:klipper_core>
        ${KLIPPER_ROOT}/scripts/buildcommands.py
        ${KLIPPER_ROOT}/src/esp32/gen_compile_time_request.py
    WORKING_DIRECTORY ${KLIPPER_ROOT}
    COMMENT "Generating klipper.dict and compile_time_request.c"
    VERBATIM)

# Link the klipper objects into the app
target_link_libraries(${COMPONENT_LIB} PRIVATE klipper_core)
