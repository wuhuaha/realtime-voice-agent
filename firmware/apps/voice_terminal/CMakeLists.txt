cmake_minimum_required(VERSION 3.16)

set(EXTRA_COMPONENT_DIRS
    "${CMAKE_CURRENT_LIST_DIR}/../../components"
)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(rva_voice_terminal)

set(FONT_ASSETS_IMAGE "${CMAKE_BINARY_DIR}/font_assets.bin")
set(FONT_ASSETS_CACHE "${CMAKE_BINARY_DIR}/font-assets-cache")
set(FONT_ASSETS_SCRIPT "${CMAKE_CURRENT_LIST_DIR}/tools/build_font_assets.py")
add_custom_command(
    OUTPUT "${FONT_ASSETS_IMAGE}"
    COMMAND "${PYTHON}" "${FONT_ASSETS_SCRIPT}"
            --output "${FONT_ASSETS_IMAGE}"
            --cache-dir "${FONT_ASSETS_CACHE}"
    DEPENDS "${FONT_ASSETS_SCRIPT}"
    COMMENT "Building verified full-Chinese font assets"
    VERBATIM
)
add_custom_target(font-assets DEPENDS "${FONT_ASSETS_IMAGE}")

partition_table_get_partition_info(
    FONT_ASSETS_OFFSET "--partition-name font_assets" "offset"
)
idf_component_get_property(ESPTOOL_MAIN_ARGS esptool_py FLASH_ARGS)
idf_component_get_property(ESPTOOL_SUB_ARGS esptool_py FLASH_SUB_ARGS)
esptool_py_flash_target(font-assets-flash "${ESPTOOL_MAIN_ARGS}" "${ESPTOOL_SUB_ARGS}")
esptool_py_flash_target_image(
    font-assets-flash font_assets "${FONT_ASSETS_OFFSET}" "${FONT_ASSETS_IMAGE}"
)
add_dependencies(font-assets-flash font-assets)

# A normal full-project flash carries the matching font image. app-flash remains
# intentionally application-only for iterations that do not change assets.
esptool_py_flash_target_image(
    flash font_assets "${FONT_ASSETS_OFFSET}" "${FONT_ASSETS_IMAGE}"
)
add_dependencies(flash font-assets)
