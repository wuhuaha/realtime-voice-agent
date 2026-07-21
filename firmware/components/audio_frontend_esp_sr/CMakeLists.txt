idf_component_register(
    SRCS "esp_sr_frontend.cc"
    INCLUDE_DIRS "include"
    REQUIRES
        audio_pipeline
        espressif__esp-sr
    PRIV_REQUIRES
        espressif__esp_audio_effects
)
