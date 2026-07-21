idf_component_register(
    SRCS "app_main.cc"
    REQUIRES
        audio_frontend_esp_sr
        audio_pipeline
        board_lichuang_s3
        device_config
        espressif__esp-sr
        native_runtime
        ui_lvgl
        voice_contracts
        voice_core
)
