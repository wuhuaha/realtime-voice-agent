idf_component_register(
    SRCS "font_assets.cc" "cbin_font.cc"
    INCLUDE_DIRS "include"
    REQUIRES
        esp_partition
        lvgl__lvgl
        mbedtls
)
