#include "transport_wss/esp_websocket_client_port.h"
#include "transport_wss/wss_session.h"

extern "C" void app_main() {
    rva::wss::WssSession session("smoke-request");
    (void)session.opened();
}
