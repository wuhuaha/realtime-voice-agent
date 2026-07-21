#include <algorithm>
#include <memory>
#include <string>
#include <string_view>
#include <vector>

#include <esp_log.h>
#include <model_path.h>
#include <nvs_flash.h>

#include "audio_frontend_esp_sr/esp_sr_frontend.h"
#include "audio_pipeline/audio_pipeline.h"
#include "board_lichuang_s3/board_audio_codec.h"
#include "board_lichuang_s3/board_audio_control.h"
#include "board_lichuang_s3/board_display.h"
#include "device_config/device_config.h"
#include "native_runtime/config_adapters.h"
#include "native_runtime/director_bootstrap.h"
#include "native_runtime/voice_runtime.h"
#include "ui_lvgl/voice_ui.h"

namespace {

constexpr char kTag[] = "rva_native";

class UiRuntimeEvents final : public rva::runtime::RuntimeEventSink {
public:
    explicit UiRuntimeEvents(rva::ui::VoiceUi* ui) : ui_(ui) {}

    void OnConnection(bool connected) override {
        Post({
            rva::ui::CommandKind::kSetConnection,
            static_cast<uint32_t>(connected ? rva::ui::ConnectionState::kOnline
                                            : rva::ui::ConnectionState::kOffline),
            {},
        });
        if (!connected) {
            Post({
                rva::ui::CommandKind::kSetConversation,
                static_cast<uint32_t>(rva::ui::ConversationState::kIdle),
                {},
            });
        }
    }
    void OnMediaProfile(rva::runtime::MediaPreference preference) override {
        Post({
            rva::ui::CommandKind::kSetActiveTransport,
            static_cast<uint32_t>(preference == rva::runtime::MediaPreference::kUdp
                                      ? rva::ui::Transport::kUdp
                                      : rva::ui::Transport::kWss),
            {},
        });
        Post({
            rva::ui::CommandKind::kSetConversation,
            static_cast<uint32_t>(rva::ui::ConversationState::kListening),
            {},
        });
    }
    void OnTranscript(const char* text, bool final) override {
        Post({rva::ui::CommandKind::kSetAsrText, final ? 1U : 0U, text == nullptr ? "" : text});
    }
    void OnResponseText(const char* text) override {
        Post({rva::ui::CommandKind::kSetResponseText, 0, text == nullptr ? "" : text});
    }
    void OnConversationPhase(rva::runtime::ConversationPhase phase) override {
        rva::ui::ConversationState state = rva::ui::ConversationState::kListening;
        if (phase == rva::runtime::ConversationPhase::kThinking) {
            state = rva::ui::ConversationState::kThinking;
        } else if (phase == rva::runtime::ConversationPhase::kSpeaking) {
            state = rva::ui::ConversationState::kSpeaking;
        }
        Post({
            rva::ui::CommandKind::kSetConversation,
            static_cast<uint32_t>(state),
            {},
        });
    }
    void OnFailure(const char* category) override {
        ESP_LOGW(kTag, "runtime failure category=%s", category == nullptr ? "unknown" : category);
        Post({
            rva::ui::CommandKind::kSetConnection,
            static_cast<uint32_t>(rva::ui::ConnectionState::kError),
            {},
        });
    }

private:
    void Post(rva::ui::UiCommand command) {
        if (ui_ != nullptr) ui_->Post(command);
    }

    rva::ui::VoiceUi* ui_;
};

bool InitializeNvs() {
    esp_err_t result = nvs_flash_init();
    if (result == ESP_ERR_NVS_NO_FREE_PAGES || result == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        if (nvs_flash_erase() != ESP_OK) return false;
        result = nvs_flash_init();
    }
    return result == ESP_OK;
}

std::vector<rva::config::WifiCredential> ProvisionedWifi() {
    std::vector<rva::config::WifiCredential> credentials;
    if (CONFIG_RVA_WIFI_PRIMARY_SSID[0] != '\0') {
        credentials.push_back({CONFIG_RVA_WIFI_PRIMARY_SSID, CONFIG_RVA_WIFI_PRIMARY_PASSWORD});
    }
    if (CONFIG_RVA_WIFI_FALLBACK_SSID[0] != '\0') {
        credentials.push_back({CONFIG_RVA_WIFI_FALLBACK_SSID, CONFIG_RVA_WIFI_FALLBACK_PASSWORD});
    }
    return credentials;
}

bool EndsWith(std::string_view value, std::string_view suffix) {
    return value.size() >= suffix.size() && value.substr(value.size() - suffix.size()) == suffix;
}

std::vector<rva::config::EndpointCandidate> ProvisionedEndpoints() {
    if (CONFIG_RVA_DIRECTOR_BOOTSTRAP_URL[0] == '\0') return {};
    rva::config::EndpointSnapshot parsed;
    if (rva::config::DeviceConfig::ParseEndpoint(CONFIG_RVA_DIRECTOR_BOOTSTRAP_URL, &parsed) !=
        rva::config::ConfigResult::kOk) {
        return {};
    }
    return {{
        CONFIG_RVA_DIRECTOR_BOOTSTRAP_URL,
        CONFIG_RVA_DEVICE_BOOTSTRAP_TOKEN,
        parsed.origin,
    }};
}

bool PostUi(rva::ui::VoiceUi* ui, const rva::ui::UiCommand& command) {
    if (ui == nullptr) return false;
    for (uint32_t attempt = 0; attempt < 20; ++attempt) {
        if (ui->Post(command)) return true;
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    return false;
}

void ScanWifi(rva::runtime::WifiStation* wifi, rva::ui::VoiceUi* ui) {
    if (wifi == nullptr || ui == nullptr) return;
    PostUi(ui, {.kind = rva::ui::CommandKind::kSetConfigMessage, .text = "正在扫描..."});
    std::vector<rva::runtime::WifiScanRecord> records;
    const bool success = wifi->Scan(&records);
    PostUi(ui, {.kind = rva::ui::CommandKind::kClearWifiNetworks, .value = 0, .text = {}});
    const size_t visible = std::min<size_t>(records.size(), 8);
    for (size_t index = 0; index < visible; ++index) {
        const auto& record = records[index];
        const uint32_t value = static_cast<uint8_t>(record.rssi) | (record.secured ? 0x100U : 0U);
        PostUi(ui, {.kind = rva::ui::CommandKind::kAddWifiNetwork, .value = value, .text = record.ssid});
    }
    PostUi(ui, {
        .kind = rva::ui::CommandKind::kSetConfigMessage,
        .text = success ? (records.empty() ? "未发现网络" : "") : "扫描失败，请重试",
    });
}

bool ResolveUsableEndpoint(
    rva::config::DeviceConfig* config,
    const std::vector<rva::config::EndpointCandidate>& provisioned,
    rva::config::EndpointSnapshot* endpoint) {
    return config != nullptr && endpoint != nullptr &&
           config->ResolveEndpoint(provisioned, endpoint) == rva::config::ConfigResult::kOk &&
           EndsWith(endpoint->url, "/v1/session/bootstrap") && !endpoint->token.empty();
}

bool ConnectConfiguredWifi(
    rva::config::DeviceConfig* config,
    const std::vector<rva::config::WifiCredential>& provisioned,
    rva::runtime::WifiStation* wifi) {
    rva::config::WifiPlan plan;
    return config != nullptr && wifi != nullptr &&
           config->LoadWifiPlan(provisioned, &plan) == rva::config::ConfigResult::kOk &&
           wifi->Connect(std::move(plan)) && wifi->WaitConnected(30000);
}

bool EnsureProvisioned(
    rva::config::DeviceConfig* config,
    rva::runtime::WifiStation* wifi,
    rva::ui::VoiceUi* ui,
    rva::config::EndpointSnapshot* endpoint,
    bool force_editor = false) {
    const auto provisioned_wifi = ProvisionedWifi();
    const auto provisioned_endpoints = ProvisionedEndpoints();
    bool wifi_connected = ConnectConfiguredWifi(config, provisioned_wifi, wifi);
    bool endpoint_ready = ResolveUsableEndpoint(config, provisioned_endpoints, endpoint);
    if (wifi_connected && endpoint_ready && !force_editor) return true;
    if (ui == nullptr) {
        ESP_LOGE(kTag, "Configuration is incomplete and provisioning UI is unavailable");
        return false;
    }

    const std::string initial_endpoint = endpoint_ready
                                             ? endpoint->url
                                             : (provisioned_endpoints.empty()
                                                    ? std::string{}
                                                    : provisioned_endpoints.front().url);
    PostUi(ui, {.kind = rva::ui::CommandKind::kSetEndpointDraft, .text = initial_endpoint});
    PostUi(ui, {.kind = rva::ui::CommandKind::kOpenWifi, .value = 0, .text = {}});
    ScanWifi(wifi, ui);
    if (!wifi_connected) {
        PostUi(ui, {.kind = rva::ui::CommandKind::kSetConfigMessage, .text = "请选择可用网络"});
    }

    bool exit_requested = false;
    while (!wifi_connected || !endpoint_ready || (force_editor && !exit_requested)) {
        rva::ui::UiEvent event;
        if (!ui->PollEvent(&event)) {
            vTaskDelay(pdMS_TO_TICKS(25));
            continue;
        }
        if (event.kind == rva::ui::EventKind::kRequestWifiScan) {
            ScanWifi(wifi, ui);
        } else if (event.kind == rva::ui::EventKind::kSaveWifi) {
            rva::config::WifiCredential credential{event.text, event.secret};
            event.secret.assign(event.secret.size(), '\0');
            const auto save_result = config->SaveWifi(credential);
            credential.password.assign(credential.password.size(), '\0');
            if (save_result != rva::config::ConfigResult::kOk) {
                PostUi(ui, {.kind = rva::ui::CommandKind::kSetConfigMessage,
                            .text = "SSID 或密码格式无效"});
                continue;
            }
            PostUi(ui, {.kind = rva::ui::CommandKind::kSetConfigMessage, .text = "正在连接..."});
            wifi_connected = ConnectConfiguredWifi(config, provisioned_wifi, wifi);
            PostUi(ui, {
                .kind = rva::ui::CommandKind::kSetConfigMessage,
                .text = wifi_connected ? "网络已连接" : "连接失败，请检查密码",
            });
            if (wifi_connected && !endpoint_ready) {
                PostUi(ui, {.kind = rva::ui::CommandKind::kOpenEndpoint, .text = initial_endpoint});
            }
        } else if (event.kind == rva::ui::EventKind::kSaveEndpoint) {
            if (config->SaveEndpoint(event.text) != rva::config::ConfigResult::kOk ||
                !ResolveUsableEndpoint(config, provisioned_endpoints, endpoint)) {
                PostUi(ui, {
                    .kind = rva::ui::CommandKind::kSetConfigMessage,
                    .text = "地址无效或未绑定凭据",
                });
                continue;
            }
            endpoint_ready = true;
            PostUi(ui, {.kind = rva::ui::CommandKind::kSetEndpointDraft, .text = endpoint->url});
        } else if (event.kind == rva::ui::EventKind::kExitProvisioning) {
            if (wifi_connected && endpoint_ready) {
                exit_requested = true;
            } else {
                PostUi(ui, {.kind = rva::ui::CommandKind::kOpenWifi, .value = 0, .text = {}});
                PostUi(ui, {.kind = rva::ui::CommandKind::kSetConfigMessage,
                            .text = "请先完成网络和服务配置"});
            }
        }
    }
    PostUi(ui, {.kind = rva::ui::CommandKind::kBackHome, .value = 0, .text = {}});
    return true;
}

}  // namespace

extern "C" void app_main() {
    if (!InitializeNvs()) {
        ESP_LOGE(kTag, "NVS initialization failed");
        return;
    }

    rva::board::lichuang_s3::SharedI2cBus i2c;
    rva::board::lichuang_s3::Pca9557Control pca9557;
    if (i2c.Start() != ESP_OK || pca9557.Start(i2c.handle()) != ESP_OK) {
        ESP_LOGE(kTag, "Board control initialization failed");
        return;
    }

    std::unique_ptr<rva::board::lichuang_s3::LichuangDisplay> display;
    std::unique_ptr<rva::ui::VoiceUi> ui;
#ifdef CONFIG_RVA_ENABLE_UI
    {
        display = std::make_unique<rva::board::lichuang_s3::LichuangDisplay>(i2c, pca9557);
        if (display->Start() == ESP_OK) {
            ui = std::make_unique<rva::ui::VoiceUi>(
                *display,
                rva::ui::VoiceUiConfig{
                    &lv_font_source_han_sans_sc_16_cjk,
                    &lv_font_source_han_sans_sc_16_cjk,
                    LV_FONT_DEFAULT,
                    "MIC",
                });
            if (!ui->Start()) ui.reset();
        }
    }
#endif
    UiRuntimeEvents runtime_events(ui.get());

    rva::runtime::NvsConfigStore endpoint_store;
    rva::runtime::NvsSavedWifi saved_wifi;
    rva::config::DeviceConfig device_config(endpoint_store, saved_wifi);
    rva::runtime::WifiStation wifi;
    while (!wifi.StartProvisioning()) {
        ESP_LOGE(kTag, "Wi-Fi station initialization failed; retrying");
        vTaskDelay(pdMS_TO_TICKS(3000));
    }
    rva::config::EndpointSnapshot service_endpoint;
    while (!EnsureProvisioned(&device_config, &wifi, ui.get(), &service_endpoint)) {
        vTaskDelay(pdMS_TO_TICKS(3000));
    }
    const bool clock_synchronized =
        rva::runtime::SynchronizeSystemClock(CONFIG_RVA_SNTP_SERVER, 10000);
    if (!clock_synchronized) {
        ESP_LOGW(kTag, "Clock synchronization failed; TLS or UDP grant validation may be unavailable");
    }
    const std::string device_id = rva::runtime::DeviceIdFromStationMac();
    if (device_id.empty()) {
        ESP_LOGE(kTag, "Unable to derive device identity");
        return;
    }

    srmodel_list_t* models = esp_srmodel_init("model");
    if (models == nullptr) {
        ESP_LOGW(kTag, "ESP-SR model partition unavailable; neural noise suppression disabled");
    }
    rva::audio::EspSrFrontend frontend(
        models,
        rva::audio::EspSrFrontendConfig{
            .input_sample_rate_hz = 24000,
            .enable_aec = true,
            .enable_vad = true,
            .enable_neural_noise_suppression = models != nullptr,
        });
    rva::board::lichuang_s3::LichuangAudioCodec codec(i2c, pca9557, {.output_volume = 80});
    rva::audio::AudioPipeline pipeline(codec.capture(), frontend, codec.playback());
    rva::runtime::DirectorBootstrap director;
    rva::runtime::MediaPreference preferred_media = rva::runtime::MediaPreference::kWss;
    const bool udp_available = clock_synchronized;
    bool conversation_requested = true;
    bool configuration_requested = false;

    while (true) {
        while (!conversation_requested && ui != nullptr) {
            rva::ui::UiEvent event;
            while (ui->PollEvent(&event)) {
                if (event.kind == rva::ui::EventKind::kSelectTransport ||
                    event.kind == rva::ui::EventKind::kStartConversation) {
                    preferred_media = udp_available && event.transport == rva::ui::Transport::kUdp
                                          ? rva::runtime::MediaPreference::kUdp
                                          : rva::runtime::MediaPreference::kWss;
                }
                if (event.kind == rva::ui::EventKind::kStartConversation) {
                    conversation_requested = true;
                } else if (event.kind == rva::ui::EventKind::kRequestWifiScan) {
                    EnsureProvisioned(&device_config, &wifi, ui.get(), &service_endpoint, true);
                }
            }
            if (!conversation_requested) vTaskDelay(pdMS_TO_TICKS(50));
        }
        if (!conversation_requested) conversation_requested = true;
        rva::runtime::BootstrapGrant grant;
        if (!director.Request(
                service_endpoint.url,
                service_endpoint.token,
                CONFIG_RVA_TENANT_ID,
                device_id,
                &grant)) {
            runtime_events.OnFailure("director_bootstrap");
            vTaskDelay(pdMS_TO_TICKS(3000));
            continue;
        }
        rva::runtime::VoiceRuntime runtime(
            pipeline,
            frontend,
            runtime_events,
            {
                .aec = frontend.aec_enabled(),
                .vad = frontend.vad_enabled(),
                .display = ui != nullptr,
                .touch = ui != nullptr,
            });
        if (!runtime.Start(grant, device_id, preferred_media)) {
            runtime_events.OnFailure("voice_runtime_start");
            vTaskDelay(pdMS_TO_TICKS(3000));
            continue;
        }
        while (runtime.running()) {
            if (ui != nullptr) {
                rva::ui::UiEvent event;
                while (ui->PollEvent(&event)) {
                    if (event.kind == rva::ui::EventKind::kStopConversation) {
                        conversation_requested = false;
                        runtime.Stop();
                        break;
                    } else if (event.kind == rva::ui::EventKind::kRequestWifiScan) {
                        configuration_requested = true;
                        conversation_requested = false;
                        runtime.Stop();
                        break;
                    }
                }
            }
            vTaskDelay(pdMS_TO_TICKS(50));
        }
        runtime.Stop();
        if (configuration_requested) {
            configuration_requested = false;
            EnsureProvisioned(&device_config, &wifi, ui.get(), &service_endpoint, true);
        }
        if (runtime.should_fallback_to_wss()) {
            preferred_media = rva::runtime::MediaPreference::kWss;
            runtime_events.OnFailure("udp_fallback_wss");
        }
        if (ui != nullptr) {
            ui->Post({
                rva::ui::CommandKind::kSetConversation,
                static_cast<uint32_t>(rva::ui::ConversationState::kIdle),
                {},
            });
        }
        if (conversation_requested) vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
