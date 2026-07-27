#include "audio_frontend_esp_sr/esp_sr_frontend.h"

#include <cstring>
#include <utility>

#include <esp_err.h>
#include <esp_ae_rate_cvt.h>
#include <esp_log.h>
#include <esp_wn_models.h>
#include <freertos/FreeRTOS.h>

namespace rva::audio {
namespace {

constexpr uint32_t kAfeSampleRateHz = 16000;
constexpr uint8_t kAfeInputChannels = 2;
constexpr char kInputFormat[] = "MR";
constexpr size_t kMaximumResampledSamplesPerChannel = 4096;
constexpr char kLogTag[] = "rva-afe";

}  // namespace

EspSrFrontend::EspSrFrontend(srmodel_list_t* model_list, EspSrFrontendConfig config)
    : model_list_(model_list), config_(config) {}

EspSrFrontend::~EspSrFrontend() {
    Stop();
}

PortResult EspSrFrontend::Start() {
    if (started_.load()) {
        return PortResult::kOk;
    }
    if (instance_ != nullptr || interface_ != nullptr || afe_config_ != nullptr) {
        return PortResult::kInvalidState;
    }
    if (config_.vad_min_speech_ms < 32 || config_.vad_min_noise_ms < 64) {
        return PortResult::kInvalidArgument;
    }
    if (config_.input_sample_rate_hz == 0 ||
        (config_.input_sample_rate_hz % 4000 != 0 &&
         config_.input_sample_rate_hz % 11025 != 0)) {
        return PortResult::kInvalidArgument;
    }

    // ESP32-S3 still needs headroom for Wi-Fi, UDP/WSS, Opus and LVGL. The
    // HIGH_PERF AEC path repeatedly starved the task watchdog during full voice
    // sessions on the target board, so the production default uses the bounded
    // low-cost VoIP profile and leaves higher-cost AEC for explicit lab builds.
    afe_config_ = afe_config_init(kInputFormat, model_list_, AFE_TYPE_VC, AFE_MODE_LOW_COST);
    if (afe_config_ == nullptr) {
        return PortResult::kResourceExhausted;
    }

    afe_config_->aec_init = config_.enable_aec;
    afe_config_->aec_mode = AEC_MODE_VOIP_LOW_COST;
    afe_config_->vad_init = config_.enable_vad;
    afe_config_->vad_mode = VAD_MODE_0;
    afe_config_->vad_min_speech_ms = config_.vad_min_speech_ms;
    afe_config_->vad_min_noise_ms = config_.vad_min_noise_ms;
    char* wakenet_model = nullptr;
    if (config_.enable_wakenet && model_list_ != nullptr &&
        config_.wakenet_model_name != nullptr && config_.wakenet_model_name[0] != '\0') {
        wakenet_model = esp_srmodel_filter(
            model_list_, ESP_WN_PREFIX, config_.wakenet_model_name);
    }
    afe_config_->wakenet_init = wakenet_model != nullptr;
    afe_config_->wakenet_model_name = wakenet_model;
    wakenet_available_.store(wakenet_model != nullptr);
    wake_word_index_.store(0);
    if (config_.enable_wakenet && wakenet_model == nullptr) {
        ESP_LOGW(kLogTag, "WakeNet model unavailable: requested=%s",
                 config_.wakenet_model_name == nullptr ? "<null>" : config_.wakenet_model_name);
    } else if (wakenet_model != nullptr) {
        ESP_LOGI(kLogTag, "WakeNet enabled: model=%s", wakenet_model);
    }
    afe_config_->agc_init = false;
    afe_config_->memory_alloc_mode = AFE_MEMORY_ALLOC_MORE_PSRAM;
    ESP_LOGI(kLogTag, "AFE scheduling: core=%d priority=%d ring_frames=%d",
             afe_config_->afe_perferred_core, afe_config_->afe_perferred_priority,
             afe_config_->afe_ringbuf_size);

    char* noise_suppression_model = nullptr;
    if (config_.enable_neural_noise_suppression && model_list_ != nullptr) {
        noise_suppression_model = esp_srmodel_filter(model_list_, ESP_NSNET_PREFIX, nullptr);
    }
    afe_config_->ns_init = noise_suppression_model != nullptr;
    afe_config_->ns_model_name = noise_suppression_model;
    if (noise_suppression_model != nullptr) {
        afe_config_->afe_ns_mode = AFE_NS_MODE_NET;
    }

    interface_ = esp_afe_handle_from_config(afe_config_);
    if (interface_ == nullptr) {
        afe_config_free(afe_config_);
        afe_config_ = nullptr;
        return PortResult::kInternalFailure;
    }
    instance_ = interface_->create_from_config(afe_config_);
    if (instance_ == nullptr) {
        interface_ = nullptr;
        afe_config_free(afe_config_);
        afe_config_ = nullptr;
        return PortResult::kResourceExhausted;
    }

    if (interface_->get_samp_rate(instance_) != static_cast<int>(kAfeSampleRateHz) ||
        interface_->get_feed_channel_num(instance_) != kAfeInputChannels) {
        interface_->destroy(instance_);
        instance_ = nullptr;
        interface_ = nullptr;
        afe_config_free(afe_config_);
        afe_config_ = nullptr;
        return PortResult::kInvalidState;
    }

    const size_t afe_feed_samples =
        static_cast<size_t>(interface_->get_feed_chunksize(instance_));
    if (config_.input_sample_rate_hz != kAfeSampleRateHz) {
        if ((afe_feed_samples * config_.input_sample_rate_hz) % kAfeSampleRateHz != 0) {
            Stop();
            return PortResult::kInvalidArgument;
        }
        esp_ae_rate_cvt_cfg_t resampler_config = {
            .src_rate = config_.input_sample_rate_hz,
            .dest_rate = kAfeSampleRateHz,
            .channel = kAfeInputChannels,
            .bits_per_sample = 16,
            .complexity = 2,
            .perf_type = ESP_AE_RATE_CVT_PERF_TYPE_SPEED,
        };
        if (esp_ae_rate_cvt_open(&resampler_config, &input_resampler_) != ESP_AE_ERR_OK ||
            input_resampler_ == nullptr) {
            Stop();
            return PortResult::kResourceExhausted;
        }
        const size_t input_samples_per_channel =
            afe_feed_samples * config_.input_sample_rate_hz / kAfeSampleRateHz;
        uint32_t required_output_samples = 0;
        if (input_samples_per_channel > UINT32_MAX ||
            esp_ae_rate_cvt_get_max_out_sample_num(
                input_resampler_, static_cast<uint32_t>(input_samples_per_channel),
                &required_output_samples) != ESP_AE_ERR_OK ||
            required_output_samples < afe_feed_samples ||
            required_output_samples > kMaximumResampledSamplesPerChannel) {
            ESP_LOGE(kLogTag, "invalid input resampler capacity: %lu",
                     static_cast<unsigned long>(required_output_samples));
            Stop();
            return PortResult::kInternalFailure;
        }
        const size_t payload_samples =
            static_cast<size_t>(required_output_samples) * kAfeInputChannels;
        resampled_input_.resize(payload_samples);
        resampled_capacity_samples_per_channel_ = required_output_samples;
        resampled_output_samples_per_channel_ = afe_feed_samples;
        ESP_LOGI(kLogTag, "input resampler ready: in=%u expected=%u capacity=%lu",
                 static_cast<unsigned>(input_samples_per_channel),
                 static_cast<unsigned>(afe_feed_samples),
                 static_cast<unsigned long>(required_output_samples));
    }

    vad_speech_.store(false);
    fetch_telemetry_count_ = 0;
    fetch_ring_min_ = 1.0F;
    fetch_ring_max_ = 0.0F;
    started_.store(true);
    return PortResult::kOk;
}

PortResult EspSrFrontend::Stop() {
    started_.store(false);
    vad_speech_.store(false);
    wakenet_available_.store(false);
    wake_word_index_.store(0);
    {
        std::lock_guard<std::mutex> lock(resampler_mutex_);
        if (input_resampler_ != nullptr) {
            esp_ae_rate_cvt_reset(input_resampler_);
            esp_ae_rate_cvt_close(input_resampler_);
            input_resampler_ = nullptr;
        }
        resampled_input_.clear();
        resampled_capacity_samples_per_channel_ = 0;
        resampled_output_samples_per_channel_ = 0;
    }
    if (instance_ != nullptr && interface_ != nullptr) {
        interface_->reset_buffer(instance_);
        interface_->destroy(instance_);
    }
    instance_ = nullptr;
    interface_ = nullptr;
    if (afe_config_ != nullptr) {
        afe_config_free(afe_config_);
        afe_config_ = nullptr;
    }
    return PortResult::kOk;
}

PortResult EspSrFrontend::Feed(PcmView input) {
    if (!started_.load() || instance_ == nullptr || interface_ == nullptr) {
        return PortResult::kInvalidState;
    }
    const size_t expected_samples = feed_samples_per_channel() * kAfeInputChannels;
    if (input.samples == nullptr || input.sample_rate_hz != config_.input_sample_rate_hz ||
        input.channel_count != kAfeInputChannels || input.sample_count != expected_samples) {
        return PortResult::kInvalidArgument;
    }

    const int16_t* afe_input = input.samples;
    std::lock_guard<std::mutex> lock(resampler_mutex_);
    if (input_resampler_ != nullptr) {
        uint32_t output_samples_per_channel =
            static_cast<uint32_t>(resampled_capacity_samples_per_channel_);
        const uint32_t input_samples_per_channel =
            static_cast<uint32_t>(input.sample_count / kAfeInputChannels);
        int16_t* resampled = resampled_input_.data();
        const esp_ae_err_t result = esp_ae_rate_cvt_process(
            input_resampler_, const_cast<int16_t*>(input.samples), input_samples_per_channel,
            resampled, &output_samples_per_channel);
        if (result != ESP_AE_ERR_OK ||
            output_samples_per_channel != resampled_output_samples_per_channel_) {
            return PortResult::kInternalFailure;
        }
        afe_input = resampled;
    }
    return interface_->feed(instance_, afe_input) < 0 ? PortResult::kInternalFailure
                                                      : PortResult::kOk;
}

PortResult EspSrFrontend::Fetch(MutablePcmView* output, uint32_t timeout_ms) {
    if (!started_.load() || instance_ == nullptr || interface_ == nullptr) {
        return PortResult::kInvalidState;
    }
    const size_t required_samples = fetch_samples_per_channel();
    if (output == nullptr || output->samples == nullptr ||
        output->capacity_samples < required_samples) {
        return PortResult::kInvalidArgument;
    }

    const TickType_t timeout_ticks = timeout_ms == 0 ? 0 : pdMS_TO_TICKS(timeout_ms);
    afe_fetch_result_t* result = interface_->fetch_with_delay(instance_, timeout_ticks);
    if (result == nullptr) {
        return PortResult::kTimeout;
    }
    if (result->ret_value != ESP_OK || result->data == nullptr || result->data_size < 0) {
        return PortResult::kInternalFailure;
    }

    if (config_.enable_vad) {
        const bool speech = result->vad_state == VAD_SPEECH;
        vad_speech_.store(speech);
    }
    if (wakenet_available_.load() && result->wakeup_state == WAKENET_DETECTED) {
        const uint32_t index = result->wake_word_index > 0
                                   ? static_cast<uint32_t>(result->wake_word_index)
                                   : 1U;
        wake_word_index_.store(index);
    }

    const size_t samples = static_cast<size_t>(result->data_size) / sizeof(int16_t);
    if (samples != required_samples || samples > output->capacity_samples) {
        return PortResult::kResourceExhausted;
    }
    fetch_ring_min_ = result->ringbuff_free_pct < fetch_ring_min_
                          ? result->ringbuff_free_pct
                          : fetch_ring_min_;
    fetch_ring_max_ = result->ringbuff_free_pct > fetch_ring_max_
                          ? result->ringbuff_free_pct
                          : fetch_ring_max_;
    if (++fetch_telemetry_count_ == 1000) {
        const auto level = [](const int16_t* data, size_t sample_count, size_t stride) {
            uint64_t sum = 0;
            uint32_t peak = 0;
            for (size_t index = 0; index < sample_count; ++index) {
                const int32_t sample = data[index * stride];
                const uint32_t magnitude = static_cast<uint32_t>(sample < 0 ? -sample : sample);
                sum += magnitude;
                peak = magnitude > peak ? magnitude : peak;
            }
            return std::pair<uint32_t, uint32_t>{
                sample_count == 0 ? 0U : static_cast<uint32_t>(sum / sample_count), peak};
        };
        const auto output_level = level(result->data, samples, 1);
        ESP_LOGI(kLogTag,
                 "AFE fetch window: ring_free_min=%.3f max=%.3f vad=%s "
                 "mean_abs_out=%lu peak_out=%lu",
                 static_cast<double>(fetch_ring_min_), static_cast<double>(fetch_ring_max_),
                 vad_speech_.load() ? "speech" : "noise",
                 static_cast<unsigned long>(output_level.first),
                 static_cast<unsigned long>(output_level.second));
        fetch_telemetry_count_ = 0;
        fetch_ring_min_ = 1.0F;
        fetch_ring_max_ = 0.0F;
    }
    std::memcpy(output->samples, result->data, samples * sizeof(int16_t));
    output->sample_count = samples;
    output->sample_rate_hz = kAfeSampleRateHz;
    output->channel_count = 1;
    return PortResult::kOk;
}

bool EspSrFrontend::ConsumeWakeDetection(uint32_t* wake_word_index) {
    const uint32_t detected = wake_word_index_.exchange(0);
    if (detected == 0) return false;
    if (wake_word_index != nullptr) *wake_word_index = detected;
    return true;
}

bool EspSrFrontend::SetWakeNetEnabled(bool enabled) {
    if (started_.load() || instance_ != nullptr || interface_ != nullptr || afe_config_ != nullptr) {
        return false;
    }
    config_.enable_wakenet = enabled;
    wake_word_index_.store(0);
    wakenet_available_.store(false);
    return true;
}

size_t EspSrFrontend::feed_samples_per_channel() const {
    if (instance_ == nullptr || interface_ == nullptr) {
        return 0;
    }
    const size_t afe_samples = static_cast<size_t>(interface_->get_feed_chunksize(instance_));
    return afe_samples * config_.input_sample_rate_hz / kAfeSampleRateHz;
}

size_t EspSrFrontend::fetch_samples_per_channel() const {
    if (instance_ == nullptr || interface_ == nullptr) {
        return 0;
    }
    return static_cast<size_t>(interface_->get_fetch_chunksize(instance_));
}

}  // namespace rva::audio
