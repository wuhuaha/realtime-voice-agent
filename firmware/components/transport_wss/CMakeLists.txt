idf_component_register(
    SRCS "esp_websocket_client_port.cc" "wss_owner.cc" "wss_session.cc"
    INCLUDE_DIRS "include"
    REQUIRES esp_websocket_client voice_protocol
)
