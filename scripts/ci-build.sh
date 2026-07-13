#!/bin/bash
# Test script for continuous integration.

# Stop script early on any error; check variables
set -eu

# Paths to tools installed by ci-install.sh
MAIN_DIR=${PWD}
BUILD_DIR=${PWD}/ci_build
export PATH=${BUILD_DIR}/pru-elf/bin:${PATH}
export PATH=${BUILD_DIR}/or1k-elf/bin:${PATH}
PYTHON=${BUILD_DIR}/python-env/bin/python
PYTHON2=${BUILD_DIR}/python2-env/bin/python


######################################################################
# Section grouping output message helpers
######################################################################

start_test()
{
    echo "::group::=============== $1 $2"
    set -x
}

finish_test()
{
    set +x
    echo "=============== Finished $2"
    echo "::endgroup::"
}


######################################################################
# Check for whitespace errors
######################################################################

start_test check_whitespace "Check whitespace"
./scripts/check_whitespace.sh
finish_test check_whitespace "Check whitespace"


######################################################################
# Run compile tests for several different MCU types
######################################################################

DICTDIR=${BUILD_DIR}/dict
mkdir -p ${DICTDIR}

for TARGET in test/configs/*.config ; do
    start_test mcu_compile "$TARGET"
    make clean
    make distclean
    unset CC
    cp ${TARGET} .config
    make olddefconfig
    make V=1
    size out/*.elf
    ./scripts/check-software-div.sh .config out/*.elf
    finish_test mcu_compile "$TARGET"
    cp out/klipper.dict ${DICTDIR}/$(basename ${TARGET} .config).dict
done


######################################################################
# Verify Helix host/library paths and live linuxprocess transports
######################################################################

start_test helix_library "Test intentproto library and C/Python bindings"
PATH=$(dirname ${PYTHON}):${PATH} make -C lib/intentproto clean test capi
finish_test helix_library "Test intentproto library and C/Python bindings"

start_test helix_host "Test Helix workstation host paths"
for TEST in \
    test/helix_flash_test.py \
    test/helix_status_test.py \
    test/intentproto_transport_test.py \
    test/paradigm_validator_test.py \
    test/segment_lib_test.py \
    test/session_bridge_test.py \
    test/traj_higher_order_test.py \
    test/traj_pwm_feed_test.py \
    test/traj_pwm_fitter_test.py \
    test/traj_pwm_map_test.py ; do
    ${PYTHON} ${TEST}
done
finish_test helix_host "Test Helix workstation host paths"

for TARGET in test/helix-configs/*.config ; do
    start_test helix_live "$TARGET"
    make clean
    make distclean
    unset CC
    cp ${TARGET} .config
    make olddefconfig
    make V=1
    case ${TARGET} in
        *-cv2.config)
            HELIX_REQUIRE_LIVE=1 ${PYTHON} test/console_v2_live_test.py
            HELIX_REQUIRE_LIVE=1 ${PYTHON} test/self_test_live_test.py
            ;;
        *-session.config)
            HELIX_REQUIRE_LIVE=1 ${PYTHON} test/datagram_session_live_test.py
            ;;
    esac
    finish_test helix_live "$TARGET"
done


######################################################################
# Verify klippy host software
######################################################################

start_test klippy "Test klippy import (Python3)"
$PYTHON klippy/klippy.py --import-test
finish_test klippy "Test klippy import (Python3)"

start_test klippy "Test klippy import (Python2)"
$PYTHON2 klippy/klippy.py --import-test
finish_test klippy "Test klippy import (Python2)"

start_test klippy "Test invoke klippy (Python3)"
$PYTHON scripts/test_klippy.py -d ${DICTDIR} test/klippy/*.test
finish_test klippy "Test invoke klippy (Python3)"

start_test klippy "Test invoke klippy (Python2)"
$PYTHON2 scripts/test_klippy.py -d ${DICTDIR} test/klippy/*.test
finish_test klippy "Test invoke klippy (Python2)"
