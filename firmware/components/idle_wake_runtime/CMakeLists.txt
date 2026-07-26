idf_component_register(
    SRCS "idle_wake_runtime.cc"
    INCLUDE_DIRS "include"
    REQUIRES
        audio_frontend_esp_sr
        audio_pipeline
)
