idf_component_register(
    SRCS
        "gcm_crypto.cc"
        "jitter_buffer.cc"
        "playout_queue.cc"
        "replay_window.cc"
        "udp_runtime.cc"
        "udp_session.cc"
    INCLUDE_DIRS "include"
    REQUIRES
        mbedtls
        voice_contracts
    PRIV_REQUIRES esp_timer
)
