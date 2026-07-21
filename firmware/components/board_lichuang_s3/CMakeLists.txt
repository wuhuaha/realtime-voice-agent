idf_component_register(
    SRCS
        "board_audio_codec.cc"
        "board_audio_control.cc"
        "board_display.cc"
    INCLUDE_DIRS "include"
    REQUIRES
        audio_pipeline
        esp_driver_i2c
        esp_driver_i2s
        esp_lcd
        espressif__esp_codec_dev
        espressif__esp_lcd_touch
    PRIV_REQUIRES
        esp_driver_gpio
        esp_driver_spi
        espressif__esp_lcd_touch_ft5x06
)
