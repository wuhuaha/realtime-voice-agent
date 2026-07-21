cmake_minimum_required(VERSION 3.16)

set(EXTRA_COMPONENT_DIRS
    "${CMAKE_CURRENT_LIST_DIR}/../../../transport_udp"
    "${CMAKE_CURRENT_LIST_DIR}/../../../voice_contracts"
)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(rva_udp_component_smoke)
