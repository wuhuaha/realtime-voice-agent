#pragma once

#include <cstdint>

#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/semphr.h>
#include <lvgl.h>

#include "board_lichuang_s3/board_display.h"
#include "ui_font_assets/font_assets.h"
#include "ui_lvgl/ui_state.h"

namespace rva::ui {

struct VoiceUiConfig final {
    const lv_font_t* text_font = nullptr;
    const lv_font_t* large_font = nullptr;
    const lv_font_t* icon_font = nullptr;
    const char* microphone_glyph = "MIC";
};

// The esp_lvgl_port task is the sole owner of reducer state and LVGL objects.
// Producers interact only through bounded command/event queues.
class VoiceUi final {
public:
    VoiceUi(board::lichuang_s3::LichuangDisplay& hardware, VoiceUiConfig config);
    ~VoiceUi();

    VoiceUi(const VoiceUi&) = delete;
    VoiceUi& operator=(const VoiceUi&) = delete;

    bool Start();
    bool Stop();
    bool Post(const UiCommand& command);
    bool PollEvent(UiEvent* event);

private:
    static constexpr size_t kCommandTextBytes = kMaxTranscriptBytes + 1;
    static constexpr UBaseType_t kCommandQueueCapacity = 8;
    static constexpr UBaseType_t kEventQueueCapacity = 8;
    static constexpr uint32_t kAsyncTimeoutMs = 2000;
    static constexpr uint32_t kMaxCommandsPerTick = 4;

    struct CommandPacket final {
        CommandKind kind;
        uint32_t value;
        char text[kCommandTextBytes];
    };

    struct EventPacket final {
        EventKind kind;
        Transport transport;
        char text[256];
        char secret[65];
    };

    static void InitializeAsync(void* context);
    static void DestroyAsync(void* context);
    static void CommandTimer(lv_timer_t* timer);
    static void MicClicked(lv_event_t* event);
    static void TransportClicked(lv_event_t* event);
    static void SettingsClicked(lv_event_t* event);
    static void WifiScanClicked(lv_event_t* event);
    static void WifiSelected(lv_event_t* event);
    static void EndpointClicked(lv_event_t* event);
    static void BackClicked(lv_event_t* event);
    static void KeyboardReady(lv_event_t* event);
    static void KeyboardCancelled(lv_event_t* event);

    void BuildHome();
    void DrainCommands();
    void Apply(UiCommand command);
    void Publish(const UiEvent& event);
    void Render();
    void RenderProvisioning();

    board::lichuang_s3::LichuangDisplay& hardware_;
    VoiceUiConfig config_;
    QueueHandle_t command_queue_ = nullptr;
    QueueHandle_t event_queue_ = nullptr;
    SemaphoreHandle_t async_done_ = nullptr;
    lv_display_t* display_ = nullptr;
    lv_indev_t* touch_ = nullptr;
    lv_timer_t* command_timer_ = nullptr;
    lv_obj_t* root_ = nullptr;
    lv_obj_t* connection_label_ = nullptr;
    lv_obj_t* conversation_label_ = nullptr;
    lv_obj_t* asr_label_ = nullptr;
    lv_obj_t* response_label_ = nullptr;
    lv_obj_t* microphone_button_ = nullptr;
    lv_obj_t* microphone_label_ = nullptr;
    lv_obj_t* transport_button_ = nullptr;
    lv_obj_t* transport_label_ = nullptr;
    lv_obj_t* settings_button_ = nullptr;
    lv_obj_t* provisioning_title_ = nullptr;
    lv_obj_t* config_message_label_ = nullptr;
    lv_obj_t* wifi_list_ = nullptr;
    lv_obj_t* wifi_scan_button_ = nullptr;
    lv_obj_t* endpoint_button_ = nullptr;
    lv_obj_t* back_button_ = nullptr;
    lv_obj_t* textarea_ = nullptr;
    lv_obj_t* keyboard_ = nullptr;
    UiState state_{};
    FontAssets font_assets_;
    bool started_ = false;
    bool async_success_ = false;
    bool port_initialized_ = false;
};

}  // namespace rva::ui
