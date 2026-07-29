#include <algorithm>
#include <exception>
#include <memory>
#include <new>
#include <string>
#include <string_view>
#include <vector>

#include <esp_log.h>
#include <esp_system.h>
#include <esp_timer.h>
#include <model_path.h>
#include <nvs_flash.h>

#include "audio_frontend_esp_sr/esp_sr_frontend.h"
#include "audio_pipeline/audio_pipeline.h"
#include "board_lichuang_s3/board_audio_codec.h"
#include "board_lichuang_s3/board_audio_control.h"
#include "board_lichuang_s3/board_display.h"
#include "device_config/device_config.h"
#include "idle_wake_runtime/idle_wake_runtime.h"
#include "native_runtime/config_adapters.h"
#include "native_runtime/director_bootstrap.h"
#include "native_runtime/voice_runtime.h"
#include "ui_lvgl/voice_ui.h"

namespace {

constexpr char kTag[] = "rva_native";

// ESP-IDF omits disabled bool symbols from sdkconfig.h. Undefined therefore
// means disabled; treating it as enabled makes an explicit Kconfig `n` unable
// to restore the MIC-owned production lifecycle.
#if defined(CONFIG_RVA_AUTO_START_CONVERSATION) && CONFIG_RVA_AUTO_START_CONVERSATION
constexpr bool kAutoStartConversation = true;
#else
constexpr bool kAutoStartConversation = false;
#endif

// MIC lifecycle control is opt-in during transport/audio stabilization. ESP-IDF
// omits bool symbols when they are n, so an undefined symbol must mean disabled.
#if defined(CONFIG_RVA_MIC_BUTTON_CONTROLS_SESSION) && CONFIG_RVA_MIC_BUTTON_CONTROLS_SESSION
constexpr bool kMicButtonControlsSession = true;
#else
constexpr bool kMicButtonControlsSession = false;
#endif

constexpr bool ShouldContinueConversation(
    bool auto_start,
    bool current_request,
    bool user_start_requested,
    bool session_refresh_requested,
    bool transport_fallback_requested,
    bool user_stop_requested,
    bool configuration_requested) {
    return !configuration_requested && !user_stop_requested &&
           (auto_start || current_request || user_start_requested || session_refresh_requested ||
             transport_fallback_requested);
}

constexpr bool ShouldRetryStartup(
    bool auto_start,
    bool current_request,
    bool start_requested,
    bool session_refresh_requested,
    bool user_stop_requested,
    bool configuration_requested) {
    return ShouldContinueConversation(
        auto_start, current_request,
        current_request || start_requested,
        session_refresh_requested,
        false,
        user_stop_requested,
        configuration_requested);
}

constexpr uint32_t RefreshRetryDelayMs(uint32_t consecutive_failures) {
    if (consecutive_failures == 0) return 0;
    if (consecutive_failures >= 5) return 4000;
    return 250U << (consecutive_failures - 1U);
}

static_assert(ShouldContinueConversation(false, false, false, true, false, false, false));
static_assert(ShouldContinueConversation(false, false, false, false, true, false, false));
static_assert(ShouldContinueConversation(false, false, true, false, false, false, false));
static_assert(ShouldContinueConversation(false, true, false, false, false, false, false));
static_assert(ShouldContinueConversation(true, false, false, false, false, false, false));
static_assert(!ShouldContinueConversation(false, false, false, false, false, false, false));
static_assert(!ShouldContinueConversation(true, true, true, true, true, true, false));
static_assert(!ShouldContinueConversation(true, true, true, true, true, false, true));
static_assert(ShouldRetryStartup(false, true, false, false, false, false));
static_assert(!ShouldRetryStartup(false, true, false, false, true, false));
static_assert(!ShouldRetryStartup(false, true, false, false, false, true));
static_assert(RefreshRetryDelayMs(0) == 0);
static_assert(RefreshRetryDelayMs(1) == 250);
static_assert(RefreshRetryDelayMs(4) == 2000);
static_assert(RefreshRetryDelayMs(5) == 4000);
static_assert(RefreshRetryDelayMs(100) == 4000);

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
        Post({
            rva::ui::CommandKind::kSetConversation,
            static_cast<uint32_t>(rva::ui::ConversationState::kIdle),
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

bool PostUi(
    rva::ui::VoiceUi* ui,
    const rva::ui::UiCommand& command,
    uint32_t timeout_ms = 200) {
    if (ui == nullptr) return false;
    constexpr uint32_t kRetryIntervalMs = 10;
    const uint32_t attempts = std::max<uint32_t>(
        1, (timeout_ms + kRetryIntervalMs - 1) / kRetryIntervalMs);
    for (uint32_t attempt = 0; attempt < attempts; ++attempt) {
        if (ui->Post(command)) return true;
        vTaskDelay(pdMS_TO_TICKS(kRetryIntervalMs));
    }
    return false;
}

void ReturnUiHome(rva::ui::VoiceUi* ui) {
    if (ui == nullptr) return;
    constexpr uint32_t kNavigationTimeoutMs = 2000;
    if (PostUi(
            ui,
            {.kind = rva::ui::CommandKind::kBackHome, .value = 0, .text = {}},
            kNavigationTimeoutMs)) {
        return;
    }
    ESP_LOGW(kTag, "UI navigation delivery failed command=back_home; continuing voice startup");
}

class ScopedBootstrapLease final {
public:
    ScopedBootstrapLease(
        rva::runtime::DirectorBootstrap& director,
        const rva::config::EndpointSnapshot& endpoint,
        const std::string& tenant_id,
        const std::string& device_id,
        const rva::runtime::BootstrapGrant& grant) noexcept
        : director_(director), endpoint_(endpoint), tenant_id_(tenant_id),
          device_id_(device_id), grant_(grant), active_(grant.HasReleaseIdentity()) {}

    ~ScopedBootstrapLease() { ReleaseNow(); }

    ScopedBootstrapLease(const ScopedBootstrapLease&) = delete;
    ScopedBootstrapLease& operator=(const ScopedBootstrapLease&) = delete;

    bool ReleaseNow() noexcept {
        if (!active_) return true;
        const bool released = director_.Release(
            endpoint_.url, endpoint_.token, tenant_id_, device_id_, grant_);
        if (released) active_ = false;
        return released;
    }

    bool Finalize() noexcept {
        const bool released = ReleaseNow();
        // Used immediately before endpoint reconfiguration. At this point two
        // bounded release rounds have already been attempted; disarm so the
        // destructor cannot target a newly selected Director endpoint.
        active_ = false;
        return released;
    }

private:
    rva::runtime::DirectorBootstrap& director_;
    const rva::config::EndpointSnapshot& endpoint_;
    const std::string& tenant_id_;
    const std::string& device_id_;
    const rva::runtime::BootstrapGrant& grant_;
    bool active_;
};

void ReleaseLeaseBeforeRestart(void* context) noexcept {
    if (context != nullptr) {
        static_cast<ScopedBootstrapLease*>(context)->ReleaseNow();
    }
}

void ProcessConversationUiEvents(
    rva::ui::VoiceUi* ui,
    bool udp_available,
    bool ui_controls_session,
    bool runtime_active,
    rva::runtime::MediaPreference* preferred_media,
    bool* conversation_requested,
    bool* configuration_requested,
    bool* stop_current_runtime,
    bool* start_conversation_seen = nullptr,
    bool* stop_conversation_seen = nullptr) {
    if (ui == nullptr || preferred_media == nullptr || conversation_requested == nullptr ||
        configuration_requested == nullptr) {
        return;
    }
    bool saw_start = false;
    bool saw_stop = false;
    bool saw_configuration = false;
    rva::ui::UiEvent event;
    while (ui->PollEvent(&event)) {
        const bool session_lifecycle_event =
            event.kind == rva::ui::EventKind::kStartConversation ||
            event.kind == rva::ui::EventKind::kStopConversation;
        if (session_lifecycle_event && !ui_controls_session) {
            ESP_LOGI(kTag, "Ignoring UI session lifecycle event kind=%u in auto-session mode",
                     static_cast<unsigned>(event.kind));
            continue;
        }
        if (event.kind == rva::ui::EventKind::kSelectTransport ||
            (ui_controls_session && event.kind == rva::ui::EventKind::kStartConversation)) {
            *preferred_media = udp_available && event.transport == rva::ui::Transport::kUdp
                                   ? rva::runtime::MediaPreference::kUdp
                                   : rva::runtime::MediaPreference::kWss;
        }
        if (event.kind == rva::ui::EventKind::kStartConversation) {
            ESP_LOGI(kTag, "MIC requested conversation start");
            saw_start = true;
        } else if (event.kind == rva::ui::EventKind::kStopConversation) {
            ESP_LOGI(kTag, "MIC requested conversation stop");
            saw_stop = true;
        } else if (event.kind == rva::ui::EventKind::kRequestWifiScan) {
            saw_configuration = true;
        }
    }
    if (start_conversation_seen != nullptr) {
        *start_conversation_seen = *start_conversation_seen || saw_start;
    }
    if (stop_conversation_seen != nullptr) {
        *stop_conversation_seen = *stop_conversation_seen || saw_stop;
    }

    // Configuration and stop are cancellation signals. Apply them after the
    // complete queue drain so a later start event in the same batch cannot
    // accidentally reopen a session that the user just stopped.
    if (*configuration_requested || saw_configuration) {
        *configuration_requested = true;
        *conversation_requested = false;
        if (runtime_active && stop_current_runtime != nullptr) {
            *stop_current_runtime = true;
        }
    } else if (saw_stop) {
        *conversation_requested = false;
        if (runtime_active && stop_current_runtime != nullptr) {
            *stop_current_runtime = true;
        }
    } else if (saw_start) {
        *conversation_requested = true;
    }
}

void WaitForRetryOrUi(
    rva::ui::VoiceUi* ui,
    bool udp_available,
    bool ui_controls_session,
    rva::runtime::MediaPreference* preferred_media,
    bool* conversation_requested,
    bool* configuration_requested,
    uint32_t timeout_ms,
    bool* start_conversation_seen,
    bool* stop_conversation_seen) {
    if (start_conversation_seen != nullptr) *start_conversation_seen = false;
    if (stop_conversation_seen != nullptr) *stop_conversation_seen = false;
    constexpr uint32_t kPollIntervalMs = 50;
    for (uint32_t elapsed = 0; elapsed < timeout_ms; elapsed += kPollIntervalMs) {
        ProcessConversationUiEvents(
            ui, udp_available, ui_controls_session, false, preferred_media, conversation_requested,
            configuration_requested, nullptr, start_conversation_seen, stop_conversation_seen);
        if (!*conversation_requested || *configuration_requested) return;
        vTaskDelay(pdMS_TO_TICKS(kPollIntervalMs));
    }
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
    rva::runtime::WifiStation* wifi,
    uint32_t timeout_ms = 30000) {
    if (config == nullptr || wifi == nullptr) return false;
    rva::config::WifiPlan plan;
    const auto load_result = config->LoadWifiPlan(provisioned, &plan);
    if (load_result != rva::config::ConfigResult::kOk) {
        ESP_LOGW(kTag, "Configured Wi-Fi plan unavailable result=%u",
                 static_cast<unsigned>(load_result));
        return false;
    }
    const size_t candidate_count = plan.credentials().size();
    if (!wifi->Connect(std::move(plan))) {
        ESP_LOGW(kTag, "Configured Wi-Fi connect did not start candidates=%u",
                 static_cast<unsigned>(candidate_count));
        return false;
    }
    const bool connected = wifi->WaitConnected(timeout_ms);
    ESP_LOGI(kTag, "Configured Wi-Fi result connected=%d candidates=%u timeout_ms=%lu",
             connected ? 1 : 0, static_cast<unsigned>(candidate_count),
             static_cast<unsigned long>(timeout_ms));
    return connected;
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
    if (wifi_connected && endpoint_ready && !force_editor) {
        ReturnUiHome(ui);
        return true;
    }
    ESP_LOGW(kTag, "Provisioning required wifi_connected=%d endpoint_ready=%d forced=%d",
             wifi_connected ? 1 : 0, endpoint_ready ? 1 : 0, force_editor ? 1 : 0);
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
        PostUi(ui, {.kind = rva::ui::CommandKind::kSetConfigMessage,
                    .text = "请选择可用网络，设备将自动重试"});
    } else if (!endpoint_ready) {
        PostUi(ui, {.kind = rva::ui::CommandKind::kOpenEndpoint, .text = initial_endpoint});
    }

    constexpr int64_t kWifiAutoRetryIntervalUs = 10LL * 1000LL * 1000LL;
    constexpr uint32_t kWifiAutoRetryTimeoutMs = 5000;
    int64_t next_wifi_retry_at = esp_timer_get_time() + kWifiAutoRetryIntervalUs;
    bool exit_requested = false;
    while (!wifi_connected || !endpoint_ready || (force_editor && !exit_requested)) {
        if (!wifi_connected && wifi->WaitConnected(0)) {
            wifi_connected = true;
            PostUi(ui, {.kind = rva::ui::CommandKind::kSetConfigMessage,
                        .text = "网络已自动恢复"});
            if (!endpoint_ready) {
                PostUi(ui, {.kind = rva::ui::CommandKind::kOpenEndpoint, .text = initial_endpoint});
            }
        } else if (!wifi_connected && esp_timer_get_time() >= next_wifi_retry_at) {
            PostUi(ui, {.kind = rva::ui::CommandKind::kSetConfigMessage,
                        .text = "正在重试默认网络..."});
            wifi_connected = ConnectConfiguredWifi(
                config, provisioned_wifi, wifi, kWifiAutoRetryTimeoutMs);
            next_wifi_retry_at = esp_timer_get_time() + kWifiAutoRetryIntervalUs;
            PostUi(ui, {.kind = rva::ui::CommandKind::kSetConfigMessage,
                        .text = wifi_connected ? "网络已自动恢复" : "自动重试失败，可手动选择网络"});
            if (wifi_connected && !endpoint_ready) {
                PostUi(ui, {.kind = rva::ui::CommandKind::kOpenEndpoint, .text = initial_endpoint});
            }
        }
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
            next_wifi_retry_at = esp_timer_get_time() + kWifiAutoRetryIntervalUs;
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
    ReturnUiHome(ui);
    return true;
}

}  // namespace

namespace {

void RunApplication() {
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
        const esp_err_t display_result = display->Start();
        if (display_result == ESP_OK) {
            ESP_LOGI(kTag, "Display hardware started; initializing LVGL UI");
            ui = std::make_unique<rva::ui::VoiceUi>(
                *display,
                rva::ui::VoiceUiConfig{
                    &lv_font_source_han_sans_sc_16_cjk,
                    &lv_font_source_han_sans_sc_16_cjk,
                    LV_FONT_DEFAULT,
                    "MIC",
                    kMicButtonControlsSession,
                });
            if (!ui->Start()) {
                ESP_LOGE(kTag, "Display stage failed: ui_start; continuing headless");
                ui.reset();
            }
        } else {
            ESP_LOGE(kTag, "Display stage failed: board_display_start (%s); continuing headless",
                     esp_err_to_name(display_result));
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
    // HTTPS needs a valid wall clock before certificate verification. Plain
    // HTTP can bootstrap immediately and uses refresh_after_ms for UDP grant
    // rotation, so a blocked SNTP server must not delay voice startup by 30 s.
    const uint32_t clock_sync_timeout_ms = service_endpoint.secure ? 30000 : 1000;
    const bool clock_synchronized =
        rva::runtime::SynchronizeSystemClock(CONFIG_RVA_SNTP_SERVER, clock_sync_timeout_ms);
    if (!clock_synchronized) {
        ESP_LOGW(kTag,
                 "Clock synchronization failed; HTTPS bootstrap remains unavailable, but UDP uses "
                 "monotonic refresh_after_ms while the server enforces expires_at_ms");
    }
    const std::string device_id = rva::runtime::DeviceIdFromStationMac();
    if (device_id.empty()) {
        ESP_LOGE(kTag, "Unable to derive device identity");
        return;
    }
    const std::string tenant_id = CONFIG_RVA_TENANT_ID;

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
    rva::runtime::IdleWakeRuntime idle_wake(codec.capture(), frontend);
    rva::runtime::DirectorBootstrap director;
    const bool udp_available = true;
    rva::runtime::MediaPreference preferred_media = rva::runtime::MediaPreference::kUdp;
    const bool ui_controls_session = kMicButtonControlsSession && !kAutoStartConversation;
    ESP_LOGI(kTag,
             "conversation lifecycle: auto_start=%d mic_button_controls_session=%d effective_ui_controls=%d",
             kAutoStartConversation ? 1 : 0, kMicButtonControlsSession ? 1 : 0,
             ui_controls_session ? 1 : 0);
    bool conversation_requested = kAutoStartConversation || ui == nullptr;
    bool configuration_requested = false;
    bool session_refresh_pending = false;
    uint32_t session_refresh_failures = 0;
    bool wake_word_available = false;
    bool wake_word_disabled = models == nullptr;

    while (true) {
        if (!conversation_requested) {
            session_refresh_pending = false;
            session_refresh_failures = 0;
        }
        if (!conversation_requested && !configuration_requested && !wake_word_disabled &&
            !idle_wake.started()) {
            const auto wake_result = idle_wake.Start();
            if (wake_result == rva::runtime::IdleWakeStartResult::kStarted ||
                wake_result == rva::runtime::IdleWakeStartResult::kAlreadyStarted) {
                wake_word_available = true;
            } else {
                if (idle_wake.failed()) {
                    ESP_LOGE(kTag, "Idle WakeNet failed to release audio after start failure; restarting");
                    esp_restart();
                    return;
                }
                wake_word_disabled = true;
                wake_word_available = false;
                ESP_LOGW(kTag, "Idle WakeNet unavailable result=%u; MIC remains enabled",
                         static_cast<unsigned>(wake_result));
            }
        }
        while (!conversation_requested && ui != nullptr) {
            ProcessConversationUiEvents(
                ui.get(), udp_available, ui_controls_session, false, &preferred_media, &conversation_requested,
                &configuration_requested, nullptr);
            uint32_t wake_word_index = 0;
            if (idle_wake.ConsumeWakeDetection(&wake_word_index)) {
                ESP_LOGI(kTag, "Wake word requested conversation start index=%lu",
                         static_cast<unsigned long>(wake_word_index));
                conversation_requested = true;
                PostUi(ui.get(), {
                    rva::ui::CommandKind::kSetConversation,
                    static_cast<uint32_t>(rva::ui::ConversationState::kConnecting),
                    {},
                });
            }
            if (idle_wake.failed()) {
                if (!idle_wake.Stop()) {
                    ESP_LOGE(kTag, "Idle WakeNet failed to release audio ownership; restarting");
                    esp_restart();
                    return;
                }
                wake_word_disabled = true;
                wake_word_available = false;
                ESP_LOGW(kTag, "Idle WakeNet stopped after audio failure; MIC remains enabled");
            }
            if (configuration_requested) {
                if (idle_wake.started() && !idle_wake.Stop()) {
                    ESP_LOGE(kTag, "Idle WakeNet failed to stop before provisioning; restarting");
                    esp_restart();
                    return;
                }
                configuration_requested = false;
                EnsureProvisioned(&device_config, &wifi, ui.get(), &service_endpoint, true);
                break;
            }
            if (!conversation_requested) vTaskDelay(pdMS_TO_TICKS(50));
        }
        if (idle_wake.started() && !idle_wake.Stop()) {
            ESP_LOGE(kTag, "Idle WakeNet failed to release audio ownership; restarting");
            esp_restart();
            return;
        }
        if (!conversation_requested) continue;
        // A Wi-Fi driver reconnect budget is deliberately bounded. When it is
        // exhausted, the application supervisor reloads the complete saved +
        // provisioned plan so an AP outage or network change cannot leave the
        // device permanently retrying Director over a dead station.
        if (!wifi.WaitConnected(0) &&
            !ConnectConfiguredWifi(&device_config, ProvisionedWifi(), &wifi)) {
            runtime_events.OnFailure("wifi_reconnect");
            PostUi(ui.get(), {
                rva::ui::CommandKind::kSetConversation,
                static_cast<uint32_t>(rva::ui::ConversationState::kConnecting),
                {},
            });
            uint32_t retry_delay_ms = 3000;
            if (session_refresh_pending) {
                if (session_refresh_failures < 5) ++session_refresh_failures;
                retry_delay_ms = RefreshRetryDelayMs(session_refresh_failures);
                ESP_LOGW(kTag,
                         "UDP grant refresh Wi-Fi reconnect failed; retry=%lu delay_ms=%lu",
                         static_cast<unsigned long>(session_refresh_failures),
                         static_cast<unsigned long>(retry_delay_ms));
            }
            bool retry_start_requested = false;
            bool retry_stop_requested = false;
            WaitForRetryOrUi(
                ui.get(), udp_available, ui_controls_session, &preferred_media,
                &conversation_requested, &configuration_requested, retry_delay_ms,
                &retry_start_requested, &retry_stop_requested);
            const bool retry_configuration_requested = configuration_requested;
            if (retry_configuration_requested) {
                configuration_requested = false;
                EnsureProvisioned(&device_config, &wifi, ui.get(), &service_endpoint, true);
            }
            if (ui != nullptr) {
                conversation_requested = ShouldRetryStartup(
                    kAutoStartConversation, conversation_requested, retry_start_requested,
                    session_refresh_pending, retry_stop_requested, retry_configuration_requested);
            }
            if (session_refresh_pending && !conversation_requested) {
                session_refresh_pending = false;
                session_refresh_failures = 0;
            }
            continue;
        }
        // Wi-Fi connect is synchronous. Drain cancellation before the next
        // external action so a Stop pressed during reconnect cannot bootstrap
        // a session after the network call returns.
        ProcessConversationUiEvents(
            ui.get(), udp_available, ui_controls_session, false, &preferred_media,
            &conversation_requested, &configuration_requested, nullptr);
        if (!conversation_requested || configuration_requested) continue;
        PostUi(ui.get(), {
            rva::ui::CommandKind::kSetConnection,
            static_cast<uint32_t>(rva::ui::ConnectionState::kConnecting),
            {},
        });
        PostUi(ui.get(), {
            rva::ui::CommandKind::kSetConversation,
            static_cast<uint32_t>(rva::ui::ConversationState::kConnecting),
            {},
        });
        rva::runtime::BootstrapGrant grant;
        const bool bootstrap_accepted = director.Request(
            service_endpoint.url,
            service_endpoint.token,
            tenant_id,
            device_id,
            &grant);
        ScopedBootstrapLease lease(
            director, service_endpoint, tenant_id, device_id, grant);
        if (bootstrap_accepted) {
            // The HTTP request can outlive a user cancellation. A returned
            // grant is never handed to runtime until pending UI events have
            // been drained; cancellation releases it immediately.
            ProcessConversationUiEvents(
                ui.get(), udp_available, ui_controls_session, false, &preferred_media,
                &conversation_requested, &configuration_requested, nullptr);
            if (!conversation_requested || configuration_requested) {
                lease.ReleaseNow();
                continue;
            }
        }
        if (!bootstrap_accepted) {
            lease.ReleaseNow();
            runtime_events.OnFailure("director_bootstrap");
            PostUi(ui.get(), {
                rva::ui::CommandKind::kSetConversation,
                static_cast<uint32_t>(rva::ui::ConversationState::kConnecting),
                {},
            });
            uint32_t retry_delay_ms = 3000;
            if (session_refresh_pending) {
                if (session_refresh_failures < 5) ++session_refresh_failures;
                retry_delay_ms = RefreshRetryDelayMs(session_refresh_failures);
                ESP_LOGW(kTag,
                         "UDP grant refresh bootstrap failed; retry=%lu delay_ms=%lu",
                         static_cast<unsigned long>(session_refresh_failures),
                         static_cast<unsigned long>(retry_delay_ms));
            }
            bool retry_start_requested = false;
            bool retry_stop_requested = false;
            WaitForRetryOrUi(
                ui.get(), udp_available, ui_controls_session, &preferred_media, &conversation_requested,
                &configuration_requested, retry_delay_ms, &retry_start_requested,
                &retry_stop_requested);
            const bool retry_configuration_requested = configuration_requested;
            if (retry_configuration_requested) {
                configuration_requested = false;
                lease.Finalize();
                EnsureProvisioned(&device_config, &wifi, ui.get(), &service_endpoint, true);
            }
            if (ui != nullptr) {
                conversation_requested = ShouldRetryStartup(
                    kAutoStartConversation, conversation_requested, retry_start_requested,
                    session_refresh_pending, retry_stop_requested, retry_configuration_requested);
            }
            if (session_refresh_pending && !conversation_requested) {
                session_refresh_pending = false;
                session_refresh_failures = 0;
            }
            continue;
        }
        rva::runtime::VoiceRuntime runtime(
            pipeline,
            frontend,
            runtime_events,
            {
                .aec = frontend.aec_enabled(),
                .vad = frontend.vad_enabled(),
                .wake_word = wake_word_available,
                .display = ui != nullptr,
                .touch = ui != nullptr,
            });
        runtime.SetFailClosedHook(ReleaseLeaseBeforeRestart, &lease);
        if (!runtime.Start(grant, device_id, preferred_media)) {
            runtime_events.OnFailure("voice_runtime_start");
            PostUi(ui.get(), {
                rva::ui::CommandKind::kSetConversation,
                static_cast<uint32_t>(rva::ui::ConversationState::kConnecting),
                {},
            });
            lease.ReleaseNow();
            uint32_t retry_delay_ms = 3000;
            if (session_refresh_pending) {
                if (session_refresh_failures < 5) ++session_refresh_failures;
                retry_delay_ms = RefreshRetryDelayMs(session_refresh_failures);
                ESP_LOGW(kTag,
                         "UDP grant refresh runtime start failed; retry=%lu delay_ms=%lu",
                         static_cast<unsigned long>(session_refresh_failures),
                         static_cast<unsigned long>(retry_delay_ms));
            }
            bool retry_start_requested = false;
            bool retry_stop_requested = false;
            WaitForRetryOrUi(
                ui.get(), udp_available, ui_controls_session, &preferred_media, &conversation_requested,
                &configuration_requested, retry_delay_ms, &retry_start_requested,
                &retry_stop_requested);
            const bool retry_configuration_requested = configuration_requested;
            if (retry_configuration_requested) {
                configuration_requested = false;
                lease.Finalize();
                EnsureProvisioned(&device_config, &wifi, ui.get(), &service_endpoint, true);
            }
            if (ui != nullptr) {
                conversation_requested = ShouldRetryStartup(
                    kAutoStartConversation, conversation_requested, retry_start_requested,
                    session_refresh_pending, retry_stop_requested, retry_configuration_requested);
            }
            if (session_refresh_pending && !conversation_requested) {
                session_refresh_pending = false;
                session_refresh_failures = 0;
            }
            continue;
        }
        const auto acknowledge_refresh_ready = [&]() {
            if (!session_refresh_pending || !runtime.media_ready()) return;
            ESP_LOGI(kTag, "UDP grant refresh established after %lu startup failure(s)",
                     static_cast<unsigned long>(session_refresh_failures));
            session_refresh_pending = false;
            session_refresh_failures = 0;
        };
        bool stop_current_runtime = false;
        while (runtime.running()) {
            acknowledge_refresh_ready();
            ProcessConversationUiEvents(
                ui.get(), udp_available, ui_controls_session, true, &preferred_media, &conversation_requested,
                &configuration_requested, &stop_current_runtime);
            if (stop_current_runtime || configuration_requested) {
                // This is the only supervisor-initiated normal close while a
                // runtime is active. Keep its cause visible without logging
                // per-frame transport activity.
                ESP_LOGI(kTag, "conversation runtime stop requested cause=%s",
                         configuration_requested ? "configuration" : "ui_stop");
                runtime.Stop();
            }
            vTaskDelay(pdMS_TO_TICKS(50));
        }
        // Catch media readiness that raced with the final running() poll. Once
        // Stop() begins it deliberately clears the media readiness atoms.
        acknowledge_refresh_ready();
        runtime.Stop();
        // The runtime can stop itself between UI polling ticks (for example at
        // a UDP grant refresh boundary). Drain events once more so a concurrent
        // MIC stop or configuration request wins over any automatic reopen.
        bool final_start_requested = false;
        bool final_stop_requested = false;
        ProcessConversationUiEvents(
            ui.get(), udp_available, ui_controls_session, false, &preferred_media,
            &conversation_requested, &configuration_requested, nullptr,
            &final_start_requested, &final_stop_requested);
        lease.ReleaseNow();
        const bool udp_fallback_requested =
            runtime.should_fallback_to_wss() && conversation_requested &&
            !stop_current_runtime && !final_stop_requested && !configuration_requested;
        const bool session_refresh_requested =
            runtime.should_refresh_session() && conversation_requested &&
            !udp_fallback_requested && !stop_current_runtime && !final_stop_requested &&
            !configuration_requested;
        const bool refresh_retry_requested =
            session_refresh_pending && conversation_requested &&
            !udp_fallback_requested && !stop_current_runtime && !final_stop_requested &&
            !configuration_requested;
        const bool continue_refresh = session_refresh_requested || refresh_retry_requested;
        const bool configuration_interrupted = configuration_requested;
        const char* runtime_end_cause = "transport_or_runtime_end";
        if (stop_current_runtime || final_stop_requested) {
            runtime_end_cause = "ui_stop";
        } else if (configuration_interrupted) {
            runtime_end_cause = "configuration";
        } else if (udp_fallback_requested) {
            runtime_end_cause = "udp_fallback_wss";
        } else if (session_refresh_requested) {
            runtime_end_cause = "udp_grant_refresh";
        } else if (refresh_retry_requested) {
            runtime_end_cause = "udp_refresh_retry";
        }
        ESP_LOGI(
            kTag,
            "conversation runtime ended cause=%s requested=%d refresh=%d fallback=%d",
            runtime_end_cause, conversation_requested ? 1 : 0, session_refresh_requested ? 1 : 0,
            udp_fallback_requested ? 1 : 0);
        if (configuration_interrupted) {
            configuration_requested = false;
            lease.Finalize();
            EnsureProvisioned(&device_config, &wifi, ui.get(), &service_endpoint, true);
        }
        if (udp_fallback_requested) {
            preferred_media = rva::runtime::MediaPreference::kWss;
            runtime_events.OnFailure("udp_fallback_wss");
            ESP_LOGW(kTag, "UDP media unavailable; retrying current request with fresh WSS route");
        }
        if (session_refresh_requested) {
            ESP_LOGI(kTag, "UDP grant refresh; continuing conversation with fresh bootstrap");
        }
        if (refresh_retry_requested) {
            if (session_refresh_failures < 5) ++session_refresh_failures;
            ESP_LOGW(kTag,
                     "UDP grant refresh runtime ended before media ready; retry=%lu delay_ms=%lu",
                     static_cast<unsigned long>(session_refresh_failures),
                     static_cast<unsigned long>(RefreshRetryDelayMs(session_refresh_failures)));
        }
        if (ui != nullptr) {
            conversation_requested = ShouldContinueConversation(
                kAutoStartConversation,
                conversation_requested,
                final_start_requested,
                continue_refresh,
                udp_fallback_requested,
                final_stop_requested,
                configuration_interrupted);
            if (conversation_requested) {
                ui->Post({
                    rva::ui::CommandKind::kSetConversation,
                    static_cast<uint32_t>(rva::ui::ConversationState::kConnecting),
                    {},
                });
            } else {
                ui->Post({
                    rva::ui::CommandKind::kSetConversation,
                    static_cast<uint32_t>(rva::ui::ConversationState::kIdle),
                    {},
                });
            }
        }
        if (configuration_interrupted || final_stop_requested || udp_fallback_requested) {
            session_refresh_pending = false;
            session_refresh_failures = 0;
        } else if (session_refresh_requested) {
            session_refresh_pending = true;
            session_refresh_failures = 0;
        }
        if (conversation_requested) {
            const uint32_t delay_ms = refresh_retry_requested
                                          ? RefreshRetryDelayMs(session_refresh_failures)
                                          : (session_refresh_requested ? 0U : 1000U);
            if (delay_ms > 0) {
                bool retry_start_requested = false;
                bool retry_stop_requested = false;
                WaitForRetryOrUi(
                    ui.get(), udp_available, ui_controls_session, &preferred_media,
                    &conversation_requested, &configuration_requested, delay_ms,
                    &retry_start_requested, &retry_stop_requested);
                const bool retry_configuration_requested = configuration_requested;
                conversation_requested = ShouldRetryStartup(
                    kAutoStartConversation, conversation_requested, retry_start_requested,
                    session_refresh_pending, retry_stop_requested,
                    retry_configuration_requested);
            }
        }
    }
}

}  // namespace

extern "C" void app_main() {
    try {
        RunApplication();
    } catch (const std::bad_alloc&) {
        ESP_LOGE(kTag, "fatal application allocation failure; restarting");
    } catch (const std::exception& error) {
        ESP_LOGE(kTag, "fatal application exception: %s; restarting", error.what());
    } catch (...) {
        ESP_LOGE(kTag, "fatal unknown application exception; restarting");
    }
    vTaskDelay(pdMS_TO_TICKS(50));
    esp_restart();
}
