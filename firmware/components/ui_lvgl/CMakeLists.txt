idf_component_register(
    SRCS
        "ui_reducer.cc"
        "voice_ui.cc"
    INCLUDE_DIRS "include"
    REQUIRES board_lichuang_s3
    PRIV_REQUIRES
        espressif__esp_lvgl_port
        lvgl__lvgl
)
