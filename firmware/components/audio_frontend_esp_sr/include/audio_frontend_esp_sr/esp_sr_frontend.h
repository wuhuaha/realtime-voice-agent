#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <mutex>
#include <vector>

#include <esp_afe_sr_models.h>

#include "audio_pipeline/audio_ports.h"

namespace rva::audio {

struct EspSrFrontendConfig final {
    uint32_t input_sample_rate_hz = 16000;
    bool enable_aec = true;
    bool enable_vad = false;
    bool enable_neural_noise_suppression = true;
    bool enable_wakenet = false;
    const char* wakenet_model_name = "wn9s_hiesp";
    int vad_min_speech_ms = 128;
    int vad_min_noise_ms = 100;
};

// ESP-SR owns its internal worker/ring buffers. The caller owns model_list and
// must stop and join feed/fetch tasks before calling Stop(). Input is interleaved
// MR; non-16 kHz input is converted to ESP-SR's required 16 kHz before feed.
class EspSrFrontend final : public FrontendPort {
public:
    EspSrFrontend(srmodel_list_t* model_list, EspSrFrontendConfig config = {});
    ~EspSrFrontend() override;

    EspSrFrontend(const EspSrFrontend&) = delete;
    EspSrFrontend& operator=(const EspSrFrontend&) = delete;

    PortResult Start() override;
    PortResult Stop() override;
    PortResult Feed(PcmView input) override;
    PortResult Fetch(MutablePcmView* output, uint32_t timeout_ms) override;

    [[nodiscard]] size_t feed_samples_per_channel() const;
    [[nodiscard]] size_t fetch_samples_per_channel() const;
    [[nodiscard]] bool started() const { return started_.load(); }
    [[nodiscard]] bool aec_enabled() const { return config_.enable_aec; }
    [[nodiscard]] bool vad_enabled() const { return config_.enable_vad; }
    [[nodiscard]] bool speech_active() const { return !config_.enable_vad || vad_speech_.load(); }
    [[nodiscard]] bool wakenet_available() const { return wakenet_available_.load(); }
    bool SetWakeNetEnabled(bool enabled);
    bool ConsumeWakeDetection(uint32_t* wake_word_index = nullptr);

private:
    srmodel_list_t* model_list_ = nullptr;
    EspSrFrontendConfig config_{};
    const esp_afe_sr_iface_t* interface_ = nullptr;
    esp_afe_sr_data_t* instance_ = nullptr;
    afe_config_t* afe_config_ = nullptr;
    void* input_resampler_ = nullptr;
    std::vector<int16_t> resampled_input_;
    size_t resampled_capacity_samples_per_channel_ = 0;
    size_t resampled_output_samples_per_channel_ = 0;
    std::mutex resampler_mutex_;
    std::atomic<bool> started_{false};
    std::atomic<bool> vad_speech_{false};
    std::atomic<bool> wakenet_available_{false};
    std::atomic<uint32_t> wake_word_index_{0};
};

}  // namespace rva::audio
