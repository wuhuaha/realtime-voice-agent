#include "ui_lvgl/voice_ui.h"

#include <cstring>
#include <utility>

#include <esp_log.h>
#include <esp_lvgl_port.h>
#include <esp_system.h>

#include "board_lichuang_s3/display_config.h"

namespace rva::ui {
namespace {

constexpr char kTag[] = "rva_voice_ui";
constexpr uint32_t kLvglLockTimeoutMs = 2000;

lv_color_t Background() { return lv_color_hex(0x101516); }
lv_color_t Surface() { return lv_color_hex(0x1B2324); }
lv_color_t Text() { return lv_color_hex(0xF3F7F5); }
lv_color_t Muted() { return lv_color_hex(0x91A19A); }
lv_color_t Listening() { return lv_color_hex(0x38D6A0); }
lv_color_t Speaking() { return lv_color_hex(0xFFB84D); }
lv_color_t Connecting() { return lv_color_hex(0x4FB7D8); }
lv_color_t Danger() { return lv_color_hex(0xFF6B6B); }

[[noreturn]] void RestartAfterLvglLockTimeout(const char* operation) {
    ESP_LOGE(kTag, "UI %s failed: lvgl_lock_timeout; restarting", operation);
    esp_restart();
    while (true) {
        // esp_restart() is documented not to return. Keep the failure path
        // non-returning even if a test double or future port violates that.
    }
}

void LockLvglOrRestart(const char* operation) {
    if (lvgl_port_lock(kLvglLockTimeoutMs)) {
        return;
    }

    // A timed-out owner can still be using the display and LVGL allocator.
    // Restart instead of releasing resources underneath that task.
    RestartAfterLvglLockTimeout(operation);
}

}  // namespace

VoiceUi::VoiceUi(board::lichuang_s3::LichuangDisplay& hardware, VoiceUiConfig config)
    : hardware_(hardware), config_(config) {}

VoiceUi::~VoiceUi() {
    Stop();
}

bool VoiceUi::Start() {
    if (lifecycle_.active()) {
        return true;
    }
    if (!hardware_.started() || config_.text_font == nullptr ||
        config_.large_font == nullptr || config_.icon_font == nullptr ||
        config_.microphone_glyph == nullptr) {
        ESP_LOGE(kTag, "UI start failed: precondition (display=%d fonts=%d/%d/%d glyph=%d)",
                 hardware_.started(), config_.text_font != nullptr,
                 config_.large_font != nullptr, config_.icon_font != nullptr,
                 config_.microphone_glyph != nullptr);
        return false;
    }
    if (!lifecycle_.Begin()) {
        ESP_LOGE(kTag, "UI start failed: lifecycle already stopped; restart required");
        return false;
    }

    command_queue_ = xQueueCreate(kCommandQueueCapacity, sizeof(CommandPacket));
    event_queue_ = xQueueCreate(kEventQueueCapacity, sizeof(EventPacket));
    if (command_queue_ == nullptr || event_queue_ == nullptr) {
        ESP_LOGE(kTag, "UI start failed: queue_allocation");
        Stop();
        return false;
    }

    lvgl_port_cfg_t port_config = ESP_LVGL_PORT_INIT_CONFIG();
    port_config.task_priority = 4;
    port_config.task_max_sleep_ms = 20;
    const esp_err_t port_result = lvgl_port_init(&port_config);
    if (port_result != ESP_OK) {
        ESP_LOGE(kTag, "UI start failed: lvgl_port_init (%s)", esp_err_to_name(port_result));
        Stop();
        return false;
    }
    port_initialized_ = true;

    const esp_err_t font_result = font_assets_.Initialize();
    if (font_result == ESP_OK) {
        config_.text_font = font_assets_.text_font();
        config_.large_font = font_assets_.text_font();
    } else {
        ESP_LOGW(kTag, "Full Chinese font unavailable; using built-in fallback (%s)",
                 esp_err_to_name(font_result));
    }

    const lvgl_port_display_cfg_t display_config = {
        .io_handle = hardware_.panel_io(),
        .panel_handle = hardware_.panel(),
        .control_handle = nullptr,
        .buffer_size = board::lichuang_s3::DisplayConfig::kWidth * 20U,
        .double_buffer = false,
        .trans_size = 0,
        .hres = board::lichuang_s3::DisplayConfig::kWidth,
        .vres = board::lichuang_s3::DisplayConfig::kHeight,
        .monochrome = false,
        .rotation = {
            .swap_xy = board::lichuang_s3::DisplayConfig::kSwapXy,
            .mirror_x = board::lichuang_s3::DisplayConfig::kMirrorX,
            .mirror_y = board::lichuang_s3::DisplayConfig::kMirrorY,
        },
        .rounder_cb = nullptr,
        .color_format = LV_COLOR_FORMAT_RGB565,
        .flags = {
            .buff_dma = true,
            .buff_spiram = false,
            .sw_rotate = false,
            .swap_bytes = true,
            .full_refresh = false,
            .direct_mode = false,
        },
    };
    display_ = lvgl_port_add_disp(&display_config);
    if (display_ == nullptr) {
        ESP_LOGE(kTag, "UI start failed: lvgl_port_add_disp");
        Stop();
        return false;
    }

    if (hardware_.touch() != nullptr) {
        const lvgl_port_touch_cfg_t touch_config = {
            .disp = display_,
            .handle = hardware_.touch(),
            .scale = {.x = 1.0F, .y = 1.0F},
        };
        touch_ = lvgl_port_add_touch(&touch_config);
        if (touch_ == nullptr) {
            ESP_LOGE(kTag, "UI start failed: lvgl_port_add_touch");
            Stop();
            return false;
        }
    }

    LockLvglOrRestart("start");
    BuildHome();
    command_timer_ = lv_timer_create(CommandTimer, 10, this);
    const bool initialized = root_ != nullptr && command_timer_ != nullptr;
    lvgl_port_unlock();
    if (!initialized) {
        ESP_LOGE(kTag, "UI start failed: invalid_root_or_timer");
        Stop();
        return false;
    }
    ESP_LOGI(kTag, "UI started (touch=%d)", touch_ != nullptr);
    return true;
}

bool VoiceUi::Stop() {
    bool success = true;
    if (port_initialized_ && (root_ != nullptr || command_timer_ != nullptr)) {
        LockLvglOrRestart("stop");
        if (command_timer_ != nullptr) {
            lv_timer_delete(command_timer_);
            command_timer_ = nullptr;
        }
        if (root_ != nullptr) {
            lv_obj_delete(root_);
            root_ = nullptr;
        }
        lvgl_port_unlock();
    }
    if (touch_ != nullptr) {
        success = lvgl_port_remove_touch(touch_) == ESP_OK && success;
        touch_ = nullptr;
    }
    if (display_ != nullptr) {
        success = lvgl_port_remove_disp(display_) == ESP_OK && success;
        display_ = nullptr;
    }
    // The binary-font descriptor uses LVGL allocation and must be destroyed
    // after all objects but before the LVGL allocator is deinitialized.
    font_assets_.Deinitialize();
    if (port_initialized_) {
        const esp_err_t result = lvgl_port_deinit();
        success = (result == ESP_OK || result == ESP_ERR_INVALID_STATE) && success;
        port_initialized_ = false;
    }
    if (command_queue_ != nullptr) {
        vQueueDelete(command_queue_);
        command_queue_ = nullptr;
    }
    if (event_queue_ != nullptr) {
        vQueueDelete(event_queue_);
        event_queue_ = nullptr;
    }
    root_ = nullptr;
    command_timer_ = nullptr;
    lifecycle_.End();
    return success;
}

bool VoiceUi::Post(const UiCommand& command) {
    if (!lifecycle_.active() || command_queue_ == nullptr) {
        return false;
    }
    CommandPacket packet = {.kind = command.kind, .value = command.value, .text = {}};
    const std::string text = TruncateUtf8(command.text, kCommandTextBytes - 1);
    std::memcpy(packet.text, text.data(), text.size());
    if (xQueueSend(command_queue_, &packet, 0) != pdTRUE) {
        return false;
    }
    lvgl_port_task_wake(LVGL_PORT_EVENT_USER, nullptr);
    return true;
}

bool VoiceUi::PollEvent(UiEvent* event) {
    if (event == nullptr || event_queue_ == nullptr) {
        return false;
    }
    EventPacket packet{};
    if (xQueueReceive(event_queue_, &packet, 0) != pdTRUE) {
        return false;
    }
    *event = {
        .kind = packet.kind,
        .transport = packet.transport,
        .text = packet.text,
        .secret = packet.secret,
    };
    std::memset(packet.secret, 0, sizeof(packet.secret));
    return true;
}

void VoiceUi::CommandTimer(lv_timer_t* timer) {
    static_cast<VoiceUi*>(lv_timer_get_user_data(timer))->DrainCommands();
}

void VoiceUi::MicClicked(lv_event_t* event) {
    auto* self = static_cast<VoiceUi*>(lv_event_get_user_data(event));
    if (!self->config_.microphone_lifecycle_enabled) {
        ESP_LOGI(kTag, "MIC button pressed; session lifecycle is disabled");
        return;
    }
    ESP_LOGI(kTag, "MIC button pressed");
    self->Apply({.kind = CommandKind::kMicPressed, .value = 0, .text = {}});
}

void VoiceUi::TransportClicked(lv_event_t* event) {
    auto* self = static_cast<VoiceUi*>(lv_event_get_user_data(event));
    self->Apply({.kind = CommandKind::kTransportPressed, .value = 0, .text = {}});
}

void VoiceUi::SettingsClicked(lv_event_t* event) {
    auto* self = static_cast<VoiceUi*>(lv_event_get_user_data(event));
    self->Apply({.kind = CommandKind::kSettingsPressed, .value = 0, .text = {}});
}

void VoiceUi::WifiScanClicked(lv_event_t* event) {
    auto* self = static_cast<VoiceUi*>(lv_event_get_user_data(event));
    self->Publish({.kind = EventKind::kRequestWifiScan, .transport = Transport::kWss,
                   .text = {}, .secret = {}});
}

void VoiceUi::WifiSelected(lv_event_t* event) {
    auto* self = static_cast<VoiceUi*>(lv_event_get_user_data(event));
    auto* network = static_cast<WifiNetwork*>(lv_obj_get_user_data(lv_event_get_target_obj(event)));
    if (network != nullptr) {
        const std::string ssid = network->ssid;
        self->Apply({.kind = CommandKind::kOpenWifiKeyboard, .text = ssid});
    }
}

void VoiceUi::EndpointClicked(lv_event_t* event) {
    auto* self = static_cast<VoiceUi*>(lv_event_get_user_data(event));
    self->Apply({.kind = CommandKind::kOpenEndpoint, .text = self->state_.endpoint_draft});
}

void VoiceUi::BackClicked(lv_event_t* event) {
    auto* self = static_cast<VoiceUi*>(lv_event_get_user_data(event));
    self->Apply({.kind = CommandKind::kBackHome, .value = 0, .text = {}});
    self->Publish({.kind = EventKind::kExitProvisioning, .transport = Transport::kWss,
                   .text = {}, .secret = {}});
}

void VoiceUi::KeyboardReady(lv_event_t* event) {
    auto* self = static_cast<VoiceUi*>(lv_event_get_user_data(event));
    const char* value = lv_textarea_get_text(self->textarea_);
    if (self->state_.page == Page::kWifiKeyboard) {
        self->Publish({
            .kind = EventKind::kSaveWifi,
            .text = self->state_.selected_ssid,
            .secret = value == nullptr ? "" : value,
        });
        lv_textarea_set_text(self->textarea_, "");
    } else if (self->state_.page == Page::kEndpoint) {
        self->Publish({.kind = EventKind::kSaveEndpoint, .transport = Transport::kWss,
                       .text = value == nullptr ? "" : value, .secret = {}});
    }
}

void VoiceUi::KeyboardCancelled(lv_event_t* event) {
    auto* self = static_cast<VoiceUi*>(lv_event_get_user_data(event));
    lv_textarea_set_text(self->textarea_, "");
    self->Apply({.kind = CommandKind::kOpenWifi, .value = 0, .text = {}});
}

void VoiceUi::BuildHome() {
    root_ = lv_obj_create(lv_screen_active());
    lv_obj_set_size(root_, LV_PCT(100), LV_PCT(100));
    lv_obj_set_style_bg_color(root_, Background(), 0);
    lv_obj_set_style_border_width(root_, 0, 0);
    lv_obj_set_style_radius(root_, 0, 0);
    lv_obj_set_style_pad_all(root_, 10, 0);
    lv_obj_set_style_text_font(root_, config_.text_font, 0);
    lv_obj_clear_flag(root_, LV_OBJ_FLAG_SCROLLABLE);

    auto* brand = lv_label_create(root_);
    lv_label_set_text(brand, "AI");
    lv_obj_set_style_text_font(brand, config_.large_font, 0);
    lv_obj_set_style_text_color(brand, Text(), 0);
    lv_obj_align(brand, LV_ALIGN_TOP_LEFT, 2, 0);

    connection_label_ = lv_label_create(root_);
    lv_obj_align(connection_label_, LV_ALIGN_TOP_RIGHT, -2, 5);

    conversation_label_ = lv_label_create(root_);
    lv_obj_set_style_text_color(conversation_label_, Muted(), 0);
    lv_obj_align(conversation_label_, LV_ALIGN_TOP_MID, 0, 5);

    auto* user_role = lv_label_create(root_);
    lv_label_set_text(user_role, "我");
    lv_obj_set_style_text_color(user_role, Listening(), 0);
    lv_obj_align(user_role, LV_ALIGN_TOP_LEFT, 2, 42);

    asr_label_ = lv_label_create(root_);
    lv_obj_set_size(asr_label_, 194, 55);
    lv_label_set_long_mode(asr_label_, LV_LABEL_LONG_WRAP);
    lv_obj_set_style_text_color(asr_label_, Text(), 0);
    lv_obj_align(asr_label_, LV_ALIGN_TOP_LEFT, 2, 64);

    auto* divider = lv_obj_create(root_);
    lv_obj_set_size(divider, 194, 1);
    lv_obj_set_style_bg_color(divider, lv_color_hex(0x33403C), 0);
    lv_obj_set_style_border_width(divider, 0, 0);
    lv_obj_align(divider, LV_ALIGN_TOP_LEFT, 2, 122);

    auto* assistant_role = lv_label_create(root_);
    lv_label_set_text(assistant_role, "AI");
    lv_obj_set_style_text_color(assistant_role, Speaking(), 0);
    lv_obj_align(assistant_role, LV_ALIGN_TOP_LEFT, 2, 132);

    response_label_ = lv_label_create(root_);
    lv_obj_set_size(response_label_, 194, 70);
    lv_label_set_long_mode(response_label_, LV_LABEL_LONG_WRAP);
    lv_obj_set_style_text_color(response_label_, Text(), 0);
    lv_obj_align(response_label_, LV_ALIGN_TOP_LEFT, 2, 154);

    transport_button_ = lv_button_create(root_);
    lv_obj_set_size(transport_button_, 76, 32);
    lv_obj_align(transport_button_, LV_ALIGN_TOP_RIGHT, -8, 50);
    lv_obj_set_style_radius(transport_button_, 6, 0);
    lv_obj_set_style_bg_color(transport_button_, Surface(), 0);
    lv_obj_set_style_border_width(transport_button_, 1, 0);
    lv_obj_set_style_pad_all(transport_button_, 0, 0);
    lv_obj_add_event_cb(transport_button_, TransportClicked, LV_EVENT_CLICKED, this);
    transport_label_ = lv_label_create(transport_button_);
    lv_obj_center(transport_label_);

    microphone_button_ = lv_button_create(root_);
    lv_obj_set_size(microphone_button_, 88, 88);
    lv_obj_align(microphone_button_, LV_ALIGN_RIGHT_MID, -2, 28);
    lv_obj_set_style_radius(microphone_button_, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_border_width(microphone_button_, 3, 0);
    lv_obj_set_style_shadow_width(microphone_button_, 10, 0);
    lv_obj_set_style_shadow_opa(microphone_button_, LV_OPA_30, 0);
    lv_obj_add_event_cb(microphone_button_, MicClicked, LV_EVENT_PRESSED, this);
    microphone_label_ = lv_label_create(microphone_button_);
    lv_obj_set_style_text_font(microphone_label_, config_.icon_font, 0);
    lv_label_set_text(microphone_label_, config_.microphone_glyph);
    lv_obj_center(microphone_label_);

    settings_button_ = lv_button_create(root_);
    lv_obj_set_size(settings_button_, 76, 28);
    lv_obj_align(settings_button_, LV_ALIGN_BOTTOM_RIGHT, -8, -2);
    lv_obj_set_style_radius(settings_button_, 6, 0);
    lv_obj_set_style_bg_color(settings_button_, Surface(), 0);
    lv_obj_set_style_pad_all(settings_button_, 0, 0);
    lv_obj_add_event_cb(settings_button_, SettingsClicked, LV_EVENT_CLICKED, this);
    auto* settings_label = lv_label_create(settings_button_);
    lv_label_set_text(settings_label, "设置");
    lv_obj_center(settings_label);

    provisioning_title_ = lv_label_create(root_);
    lv_obj_set_size(provisioning_title_, 300, 20);
    lv_obj_align(provisioning_title_, LV_ALIGN_TOP_LEFT, 2, 0);

    config_message_label_ = lv_label_create(root_);
    lv_obj_set_size(config_message_label_, 292, 20);
    lv_obj_set_style_text_color(config_message_label_, Danger(), 0);
    lv_obj_set_style_text_align(config_message_label_, LV_TEXT_ALIGN_LEFT, 0);
    lv_obj_align(config_message_label_, LV_ALIGN_TOP_LEFT, 2, 20);

    wifi_list_ = lv_list_create(root_);
    lv_obj_set_size(wifi_list_, 300, 126);
    lv_obj_align(wifi_list_, LV_ALIGN_TOP_MID, 0, 42);

    wifi_scan_button_ = lv_button_create(root_);
    lv_obj_set_size(wifi_scan_button_, 86, 34);
    lv_obj_align(wifi_scan_button_, LV_ALIGN_BOTTOM_LEFT, 0, 0);
    lv_obj_add_event_cb(wifi_scan_button_, WifiScanClicked, LV_EVENT_CLICKED, this);
    auto* scan_label = lv_label_create(wifi_scan_button_);
    lv_label_set_text(scan_label, "重新扫描");
    lv_obj_center(scan_label);

    endpoint_button_ = lv_button_create(root_);
    lv_obj_set_size(endpoint_button_, 86, 34);
    lv_obj_align(endpoint_button_, LV_ALIGN_BOTTOM_MID, 0, 0);
    lv_obj_add_event_cb(endpoint_button_, EndpointClicked, LV_EVENT_CLICKED, this);
    auto* endpoint_label = lv_label_create(endpoint_button_);
    lv_label_set_text(endpoint_label, "服务地址");
    lv_obj_center(endpoint_label);

    back_button_ = lv_button_create(root_);
    lv_obj_set_size(back_button_, 86, 34);
    lv_obj_align(back_button_, LV_ALIGN_BOTTOM_RIGHT, 0, 0);
    lv_obj_add_event_cb(back_button_, BackClicked, LV_EVENT_CLICKED, this);
    auto* back_label = lv_label_create(back_button_);
    lv_label_set_text(back_label, "返回");
    lv_obj_center(back_label);

    textarea_ = lv_textarea_create(root_);
    lv_obj_set_size(textarea_, 300, 42);
    lv_obj_align(textarea_, LV_ALIGN_TOP_MID, 0, 42);
    lv_textarea_set_one_line(textarea_, true);
    lv_textarea_set_max_length(textarea_, 255);

    keyboard_ = lv_keyboard_create(root_);
    lv_obj_set_size(keyboard_, 300, 128);
    lv_obj_align(keyboard_, LV_ALIGN_BOTTOM_MID, 0, 0);
    lv_keyboard_set_textarea(keyboard_, textarea_);
    lv_obj_add_event_cb(keyboard_, KeyboardReady, LV_EVENT_READY, this);
    lv_obj_add_event_cb(keyboard_, KeyboardCancelled, LV_EVENT_CANCEL, this);
    Render();
}

void VoiceUi::DrainCommands() {
    CommandPacket packet{};
    for (uint32_t count = 0; count < kMaxCommandsPerTick; ++count) {
        if (xQueueReceive(command_queue_, &packet, 0) != pdTRUE) {
            break;
        }
        Apply({.kind = packet.kind, .value = packet.value, .text = packet.text});
    }
}

void VoiceUi::Apply(UiCommand command) {
    const std::optional<UiEvent> event = Reduce(&state_, command);
    if (event.has_value()) {
        Publish(*event);
    }
    Render();
}

void VoiceUi::Publish(const UiEvent& event) {
    EventPacket packet = {.kind = event.kind, .transport = event.transport, .text = {}, .secret = {}};
    const std::string text = TruncateUtf8(event.text, sizeof(packet.text) - 1);
    const std::string secret = TruncateUtf8(event.secret, sizeof(packet.secret) - 1);
    std::memcpy(packet.text, text.data(), text.size());
    std::memcpy(packet.secret, secret.data(), secret.size());
    if (xQueueSend(event_queue_, &packet, 0) != pdTRUE) {
        EventPacket discarded{};
        xQueueReceive(event_queue_, &discarded, 0);
        xQueueSend(event_queue_, &packet, 0);
        std::memset(discarded.secret, 0, sizeof(discarded.secret));
    }
    std::memset(packet.secret, 0, sizeof(packet.secret));
}

void VoiceUi::Render() {
    lv_label_set_text(connection_label_, ConnectionLabel(state_.connection));
    lv_obj_set_style_text_color(
        connection_label_, state_.connection == ConnectionState::kOnline
                               ? Listening()
                               : (state_.connection == ConnectionState::kError ? Danger() : Connecting()), 0);
    lv_label_set_text(conversation_label_, ConversationLabel(state_.conversation));
    lv_label_set_text(asr_label_, state_.asr_text.c_str());
    lv_label_set_text(response_label_, state_.response_text.c_str());
    lv_label_set_text(transport_label_, TransportLabel(
        state_.conversation == ConversationState::kIdle ? state_.preferred_transport
                                                        : state_.active_transport));

    const bool idle = state_.conversation == ConversationState::kIdle;
    lv_obj_set_style_bg_color(
        microphone_button_, state_.conversation == ConversationState::kListening
                                ? Listening()
                                : (state_.conversation == ConversationState::kSpeaking ? Speaking() : Surface()), 0);
    if (idle) {
        lv_obj_remove_state(transport_button_, LV_STATE_DISABLED);
    } else {
        lv_obj_add_state(transport_button_, LV_STATE_DISABLED);
    }

    const bool home = state_.page == Page::kHome;
    lv_obj_t* home_objects[] = {
        connection_label_, conversation_label_, asr_label_, response_label_, microphone_button_,
        transport_button_, settings_button_};
    for (lv_obj_t* object : home_objects) {
        if (home) lv_obj_remove_flag(object, LV_OBJ_FLAG_HIDDEN);
        else lv_obj_add_flag(object, LV_OBJ_FLAG_HIDDEN);
    }
    RenderProvisioning();
}

void VoiceUi::RenderProvisioning() {
    const bool wifi = state_.page == Page::kWifi;
    const bool editor = state_.page == Page::kWifiKeyboard || state_.page == Page::kEndpoint;
    lv_obj_t* shared_objects[] = {provisioning_title_, config_message_label_};
    for (lv_obj_t* object : shared_objects) {
        if (state_.page == Page::kHome) lv_obj_add_flag(object, LV_OBJ_FLAG_HIDDEN);
        else lv_obj_remove_flag(object, LV_OBJ_FLAG_HIDDEN);
    }
    lv_obj_t* wifi_objects[] = {wifi_list_, wifi_scan_button_, endpoint_button_, back_button_};
    for (lv_obj_t* object : wifi_objects) {
        if (wifi) lv_obj_remove_flag(object, LV_OBJ_FLAG_HIDDEN);
        else lv_obj_add_flag(object, LV_OBJ_FLAG_HIDDEN);
    }
    lv_obj_t* editor_objects[] = {textarea_, keyboard_};
    for (lv_obj_t* object : editor_objects) {
        if (editor) lv_obj_remove_flag(object, LV_OBJ_FLAG_HIDDEN);
        else lv_obj_add_flag(object, LV_OBJ_FLAG_HIDDEN);
    }
    if (state_.page == Page::kHome) return;

    lv_label_set_text(config_message_label_, state_.config_message.c_str());
    if (wifi) {
        lv_label_set_text(provisioning_title_, "选择 Wi-Fi");
        lv_obj_clean(wifi_list_);
        if (state_.wifi_networks.empty()) {
            lv_list_add_text(wifi_list_, "未发现网络，请重新扫描");
        } else {
            for (auto& network : state_.wifi_networks) {
                const std::string label = network.ssid + "  " + std::to_string(network.rssi) + " dBm";
                lv_obj_t* button = lv_list_add_button(wifi_list_, nullptr, label.c_str());
                lv_obj_set_user_data(button, &network);
                lv_obj_add_event_cb(button, WifiSelected, LV_EVENT_CLICKED, this);
            }
        }
        return;
    }

    const bool password = state_.page == Page::kWifiKeyboard;
    const std::string title = password ? "输入 Wi-Fi 密码：" + state_.selected_ssid : "配置服务地址";
    lv_label_set_text(provisioning_title_, title.c_str());
    lv_textarea_set_password_mode(textarea_, password);
    lv_textarea_set_max_length(textarea_, password ? 64 : 255);
    lv_textarea_set_placeholder_text(
        textarea_, password ? "开放网络可留空" : "https://host/v1/session/bootstrap");
    if (!password && std::string(lv_textarea_get_text(textarea_)) != state_.endpoint_draft) {
        lv_textarea_set_text(textarea_, state_.endpoint_draft.c_str());
    }
}

}  // namespace rva::ui
