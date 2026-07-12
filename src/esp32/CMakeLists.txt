# ESP-IDF project file for the klipper ESP32 target (RFC 0001 doc 07)
#
# Build:  . $IDF_PATH/export.sh && idf.py set-target esp32 && idf.py build
# See docs/ESP32.md for the full recipe (WiFi credentials, PSK
# provisioning, and the udp_bridge.py host side).
cmake_minimum_required(VERSION 3.16)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(klipper)
