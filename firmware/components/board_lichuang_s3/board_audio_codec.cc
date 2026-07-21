#include "board_lichuang_s3/board_audio_codec.h"

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <limits>

#include <driver/i2s_std.h>
#include <driver/i2s_tdm.h>
#include <esp_codec_dev_defaults.h>
#include <esp_log.h>

#include "board_lichuang_s3/audio_config.h"

namespace rva::board::lichuang_s3 {
namespace {

constexpr int kDmaDescriptorCount = 6;
constexpr int kDmaFrameCount = 240;
constexpr char kTag[] = "lichuang_codec";
constexpr int kEs7210Mic1GainRegister = 0x43;
constexpr int kEs7210Mic2GainRegister = 0x44;
constexpr int kEs7210Mic3GainRegister = 0x45;
constexpr int kEs7210GainFieldMask = 0x0F;
constexpr int kEs7210NearEndGainValue = 0x0E;
constexpr int kEs7210ReferenceGainValue = 0x00;

audio::PortResult CodecResult(int result) {
    return result == ESP_CODEC_DEV_OK ? audio::PortResult::kOk : audio::PortResult::kIoFailure;
}

audio::PortResult EspResult(esp_err_t result) {
    if (result == ESP_OK) {
        return audio::PortResult::kOk;
    }
    if (result == ESP_ERR_TIMEOUT) {
        return audio::PortResult::kTimeout;
    }
    if (result == ESP_ERR_NO_MEM) {
        return audio::PortResult::kResourceExhausted;
    }
    return audio::PortResult::kIoFailure;
}

}  // namespace

LichuangAudioCodec::LichuangAudioCodec(SharedI2cBus& i2c_bus,
                                       Pca9557Control& pca9557,
                                       CodecRuntimeConfig config)
    : i2c_bus_(i2c_bus),
      pca9557_(pca9557),
      config_(config),
      capture_endpoint_(*this),
      playback_endpoint_(*this) {}

LichuangAudioCodec::~LichuangAudioCodec() {
    Shutdown();
}

int LichuangAudioCodec::FixedDataOpen(
    const audio_codec_data_if_t* data_if, void*, int) {
    auto* fixed = reinterpret_cast<FixedI2sDataInterface*>(
        const_cast<audio_codec_data_if_t*>(data_if));
    if (fixed == nullptr || fixed->delegate == nullptr) {
        return ESP_CODEC_DEV_INVALID_ARG;
    }
    fixed->opened = true;
    return ESP_CODEC_DEV_OK;
}

bool LichuangAudioCodec::FixedDataIsOpen(const audio_codec_data_if_t* data_if) {
    const auto* fixed = reinterpret_cast<const FixedI2sDataInterface*>(data_if);
    return fixed != nullptr && fixed->delegate != nullptr && fixed->opened;
}

int LichuangAudioCodec::FixedDataEnable(
    const audio_codec_data_if_t* data_if, esp_codec_dev_type_t device_type, bool) {
    const auto* fixed = reinterpret_cast<const FixedI2sDataInterface*>(data_if);
    return fixed != nullptr && fixed->delegate != nullptr && fixed->opened &&
                   (device_type & ESP_CODEC_DEV_TYPE_IN_OUT) != ESP_CODEC_DEV_TYPE_NONE
               ? ESP_CODEC_DEV_OK
               : ESP_CODEC_DEV_INVALID_ARG;
}

int LichuangAudioCodec::FixedDataSetFormat(
    const audio_codec_data_if_t* data_if,
    esp_codec_dev_type_t device_type,
    esp_codec_dev_sample_info_t* format) {
    const auto* fixed = reinterpret_cast<const FixedI2sDataInterface*>(data_if);
    if (fixed == nullptr || fixed->delegate == nullptr || !fixed->opened || format == nullptr ||
        format->bits_per_sample != AudioConfig::kBitsPerSample ||
        format->sample_rate != AudioConfig::kInputSampleRateHz) {
        return ESP_CODEC_DEV_INVALID_ARG;
    }

    const bool playback = (device_type & ESP_CODEC_DEV_TYPE_OUT) != 0;
    const bool capture = (device_type & ESP_CODEC_DEV_TYPE_IN) != 0;
    const uint16_t selected_channels = static_cast<uint16_t>(
        ESP_CODEC_DEV_MAKE_CHANNEL_MASK(0) | ESP_CODEC_DEV_MAKE_CHANNEL_MASK(1));
    const bool playback_format = playback && !capture && format->channel == 2 &&
                                 (format->channel_mask == 0 ||
                                  format->channel_mask == selected_channels);
    const bool capture_format = capture && !playback &&
                                format->channel == AudioConfig::kRxTdmSlots &&
                                format->channel_mask == selected_channels;
    return playback_format || capture_format ? ESP_CODEC_DEV_OK : ESP_CODEC_DEV_NOT_SUPPORT;
}

int LichuangAudioCodec::FixedDataRead(
    const audio_codec_data_if_t* data_if, uint8_t* data, int size) {
    const auto* fixed = reinterpret_cast<const FixedI2sDataInterface*>(data_if);
    return fixed == nullptr || fixed->delegate == nullptr || fixed->delegate->read == nullptr
               ? ESP_CODEC_DEV_INVALID_ARG
               : fixed->delegate->read(fixed->delegate, data, size);
}

int LichuangAudioCodec::FixedDataWrite(
    const audio_codec_data_if_t* data_if, uint8_t* data, int size) {
    const auto* fixed = reinterpret_cast<const FixedI2sDataInterface*>(data_if);
    return fixed == nullptr || fixed->delegate == nullptr || fixed->delegate->write == nullptr
               ? ESP_CODEC_DEV_INVALID_ARG
               : fixed->delegate->write(fixed->delegate, data, size);
}

int LichuangAudioCodec::FixedDataClose(const audio_codec_data_if_t* data_if) {
    const auto* fixed = reinterpret_cast<const FixedI2sDataInterface*>(data_if);
    return fixed == nullptr || fixed->delegate == nullptr ? ESP_CODEC_DEV_INVALID_ARG
                                                          : ESP_CODEC_DEV_OK;
}

audio::PortResult LichuangAudioCodec::StartCapture() {
    std::lock_guard<std::mutex> lock(lifecycle_mutex_);
    if (capture_started_.load()) {
        return audio::PortResult::kOk;
    }
    audio::PortResult result = EnsureHardwareInitialized();
    if (result != audio::PortResult::kOk) {
        return result;
    }

    esp_codec_dev_sample_info_t format = {
        .bits_per_sample = AudioConfig::kBitsPerSample,
        .channel = AudioConfig::kRxTdmSlots,
        .channel_mask = static_cast<uint16_t>(ESP_CODEC_DEV_MAKE_CHANNEL_MASK(0) |
                                              ESP_CODEC_DEV_MAKE_CHANNEL_MASK(1)),
        .sample_rate = AudioConfig::kInputSampleRateHz,
        .mclk_multiple = 0,
    };
    result = CodecResult(esp_codec_dev_open(input_device_, &format));
    if (result != audio::PortResult::kOk) {
        esp_codec_dev_close(input_device_);
        return result;
    }
    const uint16_t near_end_channels = static_cast<uint16_t>(
        ESP_CODEC_DEV_MAKE_CHANNEL_MASK(0) | ESP_CODEC_DEV_MAKE_CHANNEL_MASK(1));
    result = CodecResult(esp_codec_dev_set_in_channel_gain(
        input_device_, near_end_channels, AudioConfig::kNearEndInputGainDb));
    if (result == audio::PortResult::kOk) {
        result = CodecResult(esp_codec_dev_set_in_channel_gain(
            input_device_, ESP_CODEC_DEV_MAKE_CHANNEL_MASK(2),
            AudioConfig::kReferenceInputGainDb));
    }
    int mic1_gain = 0;
    int mic2_gain = 0;
    int mic3_gain = 0;
    if (result == audio::PortResult::kOk) {
        result = CodecResult(esp_codec_dev_read_reg(
            input_device_, kEs7210Mic1GainRegister, &mic1_gain));
    }
    if (result == audio::PortResult::kOk) {
        result = CodecResult(esp_codec_dev_read_reg(
            input_device_, kEs7210Mic2GainRegister, &mic2_gain));
    }
    if (result == audio::PortResult::kOk) {
        result = CodecResult(esp_codec_dev_read_reg(
            input_device_, kEs7210Mic3GainRegister, &mic3_gain));
    }
    if (result == audio::PortResult::kOk &&
        ((mic1_gain & kEs7210GainFieldMask) != kEs7210NearEndGainValue ||
         (mic2_gain & kEs7210GainFieldMask) != kEs7210NearEndGainValue ||
         (mic3_gain & kEs7210GainFieldMask) != kEs7210ReferenceGainValue)) {
        ESP_LOGE(
            kTag, "ES7210 gain mismatch: MIC1=0x%02x MIC2=0x%02x MIC3=0x%02x",
            mic1_gain, mic2_gain, mic3_gain);
        result = audio::PortResult::kIoFailure;
    }
    if (result != audio::PortResult::kOk) {
        esp_codec_dev_close(input_device_);
        return result;
    }
    ESP_LOGI(
        kTag, "ES7210 gains verified: MIC1=0x%02x MIC2=0x%02x MIC3(ref)=0x%02x",
        mic1_gain, mic2_gain, mic3_gain);
    capture_started_.store(true);
    return audio::PortResult::kOk;
}

audio::PortResult LichuangAudioCodec::StopCapture() {
    std::lock_guard<std::mutex> lock(lifecycle_mutex_);
    if (!capture_started_.exchange(false)) {
        return audio::PortResult::kOk;
    }
    return CodecResult(esp_codec_dev_close(input_device_));
}

audio::PortResult LichuangAudioCodec::Read(audio::MutablePcmView* destination,
                                           uint32_t timeout_ms) {
    if (!capture_started_.load()) {
        return audio::PortResult::kInvalidState;
    }
    if (destination == nullptr || destination->samples == nullptr ||
        destination->capacity_samples == 0 ||
        destination->capacity_samples > std::numeric_limits<size_t>::max() / sizeof(int16_t)) {
        return audio::PortResult::kInvalidArgument;
    }

    size_t bytes_read = 0;
    const size_t requested_bytes = destination->capacity_samples * sizeof(int16_t);
    const esp_err_t result = i2s_channel_read(
        rx_channel_, destination->samples, requested_bytes, &bytes_read, timeout_ms);
    if (result != ESP_OK) {
        destination->sample_count = 0;
        return EspResult(result);
    }
    if ((bytes_read % (sizeof(int16_t) * AudioConfig::kInputChannels)) != 0) {
        destination->sample_count = 0;
        return audio::PortResult::kIoFailure;
    }
    destination->sample_count = bytes_read / sizeof(int16_t);
    destination->sample_rate_hz = AudioConfig::kInputSampleRateHz;
    destination->channel_count = AudioConfig::kInputChannels;
    return audio::PortResult::kOk;
}

audio::PortResult LichuangAudioCodec::Write(audio::PcmView input, uint32_t timeout_ms) {
    if (!playback_started_.load()) {
        return audio::PortResult::kInvalidState;
    }
    if (input.samples == nullptr || input.sample_count == 0 ||
        input.sample_rate_hz != AudioConfig::kOutputSampleRateHz || input.channel_count != 1 ||
        input.sample_count > std::numeric_limits<size_t>::max() / sizeof(int16_t)) {
        return audio::PortResult::kInvalidArgument;
    }

    size_t offset = 0;
    while (offset < input.sample_count) {
        const size_t mono_samples = std::min(
            kPlaybackMonoChunkSamples, input.sample_count - offset);
        for (size_t index = 0; index < mono_samples; ++index) {
            const int16_t sample = input.samples[offset + index];
            playback_stereo_buffer_[index * 2] = sample;
            playback_stereo_buffer_[index * 2 + 1] = sample;
        }
        size_t bytes_written = 0;
        const size_t requested_bytes = mono_samples * 2 * sizeof(int16_t);
        const esp_err_t result = i2s_channel_write(
            tx_channel_, playback_stereo_buffer_.data(), requested_bytes,
            &bytes_written, timeout_ms);
        if (result != ESP_OK) {
            return EspResult(result);
        }
        if (bytes_written != requested_bytes) {
            return audio::PortResult::kIoFailure;
        }
        offset += mono_samples;
    }
    return audio::PortResult::kOk;
}

audio::PortResult LichuangAudioCodec::StartPlayback() {
    std::lock_guard<std::mutex> lock(lifecycle_mutex_);
    if (playback_started_.load()) {
        return audio::PortResult::kOk;
    }
    if (config_.output_volume < 0 || config_.output_volume > 100) {
        return audio::PortResult::kInvalidArgument;
    }
    audio::PortResult result = EnsureHardwareInitialized();
    if (result != audio::PortResult::kOk) {
        return result;
    }

    esp_codec_dev_sample_info_t format = {
        .bits_per_sample = AudioConfig::kBitsPerSample,
        .channel = 2,
        .channel_mask = static_cast<uint16_t>(ESP_CODEC_DEV_MAKE_CHANNEL_MASK(0) |
                                              ESP_CODEC_DEV_MAKE_CHANNEL_MASK(1)),
        .sample_rate = AudioConfig::kOutputSampleRateHz,
        .mclk_multiple = 0,
    };
    result = CodecResult(esp_codec_dev_open(output_device_, &format));
    if (result == audio::PortResult::kOk) {
        result = CodecResult(esp_codec_dev_set_out_vol(output_device_, config_.output_volume));
    }
    if (result == audio::PortResult::kOk) {
        result = EspResult(pca9557_.SetPowerAmplifierEnabled(true));
    }
    if (result != audio::PortResult::kOk) {
        pca9557_.SetPowerAmplifierEnabled(false);
        esp_codec_dev_close(output_device_);
        return result;
    }
    playback_started_.store(true);
    return audio::PortResult::kOk;
}

audio::PortResult LichuangAudioCodec::StopPlayback() {
    std::lock_guard<std::mutex> lock(lifecycle_mutex_);
    if (!playback_started_.exchange(false)) {
        return audio::PortResult::kOk;
    }
    audio::PortResult first_error = EspResult(pca9557_.SetPowerAmplifierEnabled(false));
    const audio::PortResult close_result = CodecResult(esp_codec_dev_close(output_device_));
    return first_error == audio::PortResult::kOk ? close_result : first_error;
}

audio::PortResult LichuangAudioCodec::SetOutputVolume(int volume) {
    if (volume < 0 || volume > 100) {
        return audio::PortResult::kInvalidArgument;
    }
    std::lock_guard<std::mutex> lock(lifecycle_mutex_);
    config_.output_volume = volume;
    if (!playback_started_.load()) {
        return audio::PortResult::kOk;
    }
    return CodecResult(esp_codec_dev_set_out_vol(output_device_, volume));
}

audio::PortResult LichuangAudioCodec::Shutdown() {
    audio::PortResult first_error = StopCapture();
    const audio::PortResult playback_result = StopPlayback();
    if (first_error == audio::PortResult::kOk) {
        first_error = playback_result;
    }
    std::lock_guard<std::mutex> lock(lifecycle_mutex_);
    ReleaseHardware();
    return first_error;
}

audio::PortResult LichuangAudioCodec::EnsureHardwareInitialized() {
    if (input_device_ != nullptr && output_device_ != nullptr) {
        return audio::PortResult::kOk;
    }
    if (!i2c_bus_.started() || !pca9557_.started()) {
        return audio::PortResult::kInvalidState;
    }

    i2s_chan_config_t channel_config = {
        .id = static_cast<i2s_port_t>(AudioConfig::kI2sPort),
        .role = I2S_ROLE_MASTER,
        .dma_desc_num = kDmaDescriptorCount,
        .dma_frame_num = kDmaFrameCount,
        .auto_clear_after_cb = true,
        .auto_clear_before_cb = false,
        .allow_pd = false,
        .intr_priority = 0,
    };
    if (i2s_new_channel(&channel_config, &tx_channel_, &rx_channel_) != ESP_OK) {
        ReleaseHardware();
        return audio::PortResult::kResourceExhausted;
    }

    i2s_std_slot_config_t output_slots = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(
        I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO);
    // TX and RX share BCLK/WS. 2x32-bit TX and 4x16-bit RX therefore both
    // use the board-verified 64-bit frame.
    output_slots.slot_bit_width = I2S_SLOT_BIT_WIDTH_32BIT;
    output_slots.ws_width = 32;
    i2s_std_config_t output_config = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(AudioConfig::kOutputSampleRateHz),
        .slot_cfg = output_slots,
        .gpio_cfg = {
            .mclk = static_cast<gpio_num_t>(AudioConfig::kI2sMclkGpio),
            .bclk = static_cast<gpio_num_t>(AudioConfig::kI2sBclkGpio),
            .ws = static_cast<gpio_num_t>(AudioConfig::kI2sWordSelectGpio),
            .dout = static_cast<gpio_num_t>(AudioConfig::kI2sDataOutGpio),
            .din = I2S_GPIO_UNUSED,
            .invert_flags = {},
        },
    };
    output_config.clk_cfg.mclk_multiple = I2S_MCLK_MULTIPLE_256;
    output_config.slot_cfg.slot_mask = I2S_STD_SLOT_BOTH;

    const i2s_tdm_slot_mask_t capture_slots =
        static_cast<i2s_tdm_slot_mask_t>(I2S_TDM_SLOT0 | I2S_TDM_SLOT1);
    i2s_tdm_config_t input_config = {
        .clk_cfg = I2S_TDM_CLK_DEFAULT_CONFIG(AudioConfig::kInputSampleRateHz),
        .slot_cfg = I2S_TDM_PHILIPS_SLOT_DEFAULT_CONFIG(
            I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO, capture_slots),
        .gpio_cfg = {
            .mclk = static_cast<gpio_num_t>(AudioConfig::kI2sMclkGpio),
            .bclk = static_cast<gpio_num_t>(AudioConfig::kI2sBclkGpio),
            .ws = static_cast<gpio_num_t>(AudioConfig::kI2sWordSelectGpio),
            .dout = I2S_GPIO_UNUSED,
            .din = static_cast<gpio_num_t>(AudioConfig::kI2sDataInGpio),
            .invert_flags = {},
        },
    };
    input_config.clk_cfg.mclk_multiple = I2S_MCLK_MULTIPLE_256;
    input_config.clk_cfg.bclk_div = 8;
    input_config.slot_cfg.total_slot = AudioConfig::kRxTdmSlots;

    if (i2s_channel_init_std_mode(tx_channel_, &output_config) != ESP_OK ||
        i2s_channel_init_tdm_mode(rx_channel_, &input_config) != ESP_OK ||
        i2s_channel_enable(tx_channel_) != ESP_OK ||
        i2s_channel_enable(rx_channel_) != ESP_OK) {
        ReleaseHardware();
        return audio::PortResult::kIoFailure;
    }

    audio_codec_i2s_cfg_t i2s_config = {
        .port = static_cast<i2s_port_t>(AudioConfig::kI2sPort),
        .rx_handle = rx_channel_,
        .tx_handle = tx_channel_,
        .clk_src = I2S_CLK_SRC_DEFAULT,
    };
    i2s_data_delegate_ = audio_codec_new_i2s_data(&i2s_config);
    if (i2s_data_delegate_ != nullptr) {
        fixed_data_interface_ = {
            .base = {
                .open = FixedDataOpen,
                .is_open = FixedDataIsOpen,
                .enable = FixedDataEnable,
                .set_fmt = FixedDataSetFormat,
                .read = FixedDataRead,
                .write = FixedDataWrite,
                .close = FixedDataClose,
            },
            .delegate = i2s_data_delegate_,
            .opened = true,
        };
        data_interface_ = &fixed_data_interface_.base;
    }

    audio_codec_i2c_cfg_t i2c_config = {
        .port = static_cast<i2c_port_t>(AudioConfig::kI2cPort),
        .addr = AudioConfig::kEs8311CodecApiAddress8Bit,
        .bus_handle = i2c_bus_.handle(),
    };
    output_control_interface_ = audio_codec_new_i2c_ctrl(&i2c_config);
    gpio_interface_ = audio_codec_new_gpio();
    if (data_interface_ == nullptr || output_control_interface_ == nullptr ||
        gpio_interface_ == nullptr) {
        ReleaseHardware();
        return audio::PortResult::kResourceExhausted;
    }

    es8311_codec_cfg_t output_codec_config = {};
    output_codec_config.ctrl_if = output_control_interface_;
    output_codec_config.gpio_if = gpio_interface_;
    output_codec_config.codec_mode = ESP_CODEC_DEV_WORK_MODE_DAC;
    output_codec_config.pa_pin = GPIO_NUM_NC;
    output_codec_config.use_mclk = true;
    output_codec_config.hw_gain.pa_voltage = 5.0F;
    output_codec_config.hw_gain.codec_dac_voltage = 3.3F;
    output_codec_interface_ = es8311_codec_new(&output_codec_config);

    i2c_config.addr = AudioConfig::kEs7210CodecApiAddress8Bit;
    input_control_interface_ = audio_codec_new_i2c_ctrl(&i2c_config);
    es7210_codec_cfg_t input_codec_config = {};
    input_codec_config.ctrl_if = input_control_interface_;
    input_codec_config.mic_selected =
        ES7210_SEL_MIC1 | ES7210_SEL_MIC2 | ES7210_SEL_MIC3;
    input_codec_interface_ = es7210_codec_new(&input_codec_config);
    if (output_codec_interface_ == nullptr || input_control_interface_ == nullptr ||
        input_codec_interface_ == nullptr) {
        ReleaseHardware();
        return audio::PortResult::kResourceExhausted;
    }

    esp_codec_dev_cfg_t device_config = {
        .dev_type = ESP_CODEC_DEV_TYPE_OUT,
        .codec_if = output_codec_interface_,
        .data_if = data_interface_,
    };
    output_device_ = esp_codec_dev_new(&device_config);
    device_config.dev_type = ESP_CODEC_DEV_TYPE_IN;
    device_config.codec_if = input_codec_interface_;
    input_device_ = esp_codec_dev_new(&device_config);
    if (output_device_ == nullptr || input_device_ == nullptr) {
        ReleaseHardware();
        return audio::PortResult::kResourceExhausted;
    }
    return audio::PortResult::kOk;
}

void LichuangAudioCodec::ReleaseHardware() {
    if (output_device_ != nullptr) {
        esp_codec_dev_delete(output_device_);
        output_device_ = nullptr;
    }
    if (input_device_ != nullptr) {
        esp_codec_dev_delete(input_device_);
        input_device_ = nullptr;
    }
    if (input_codec_interface_ != nullptr) {
        audio_codec_delete_codec_if(input_codec_interface_);
        input_codec_interface_ = nullptr;
    }
    if (input_control_interface_ != nullptr) {
        audio_codec_delete_ctrl_if(input_control_interface_);
        input_control_interface_ = nullptr;
    }
    if (output_codec_interface_ != nullptr) {
        audio_codec_delete_codec_if(output_codec_interface_);
        output_codec_interface_ = nullptr;
    }
    if (output_control_interface_ != nullptr) {
        audio_codec_delete_ctrl_if(output_control_interface_);
        output_control_interface_ = nullptr;
    }
    if (gpio_interface_ != nullptr) {
        audio_codec_delete_gpio_if(gpio_interface_);
        gpio_interface_ = nullptr;
    }
    data_interface_ = nullptr;
    fixed_data_interface_ = {};
    if (i2s_data_delegate_ != nullptr) {
        audio_codec_delete_data_if(i2s_data_delegate_);
        i2s_data_delegate_ = nullptr;
    }
    if (rx_channel_ != nullptr) {
        i2s_channel_disable(rx_channel_);
        i2s_del_channel(rx_channel_);
        rx_channel_ = nullptr;
    }
    if (tx_channel_ != nullptr) {
        i2s_channel_disable(tx_channel_);
        i2s_del_channel(tx_channel_);
        tx_channel_ = nullptr;
    }
}

}  // namespace rva::board::lichuang_s3
