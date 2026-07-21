#pragma once

#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <mutex>

#include <audio_codec_data_if.h>
#include <audio_codec_if.h>
#include <audio_codec_ctrl_if.h>
#include <audio_codec_gpio_if.h>
#include <driver/i2s_types.h>
#include <esp_codec_dev.h>

#include "audio_pipeline/audio_ports.h"
#include "board_lichuang_s3/board_audio_control.h"

namespace rva::board::lichuang_s3 {

struct CodecRuntimeConfig final {
    int output_volume = 70;
};

// Owns I2S0, ES8311/ES7210 codec objects and their data/control interfaces.
// The shared I2C bus and PCA9557 PA control outlive this object.
class LichuangAudioCodec final {
public:
    class CaptureEndpoint final : public audio::CapturePort {
    public:
        explicit CaptureEndpoint(LichuangAudioCodec& owner) : owner_(owner) {}
        audio::PortResult Start() override { return owner_.StartCapture(); }
        audio::PortResult Stop() override { return owner_.StopCapture(); }
        audio::PortResult Read(audio::MutablePcmView* destination, uint32_t timeout_ms) override {
            return owner_.Read(destination, timeout_ms);
        }
    private:
        LichuangAudioCodec& owner_;
    };

    class PlaybackEndpoint final : public audio::PlaybackPort {
    public:
        explicit PlaybackEndpoint(LichuangAudioCodec& owner) : owner_(owner) {}
        audio::PortResult Start() override { return owner_.StartPlayback(); }
        audio::PortResult Stop() override { return owner_.StopPlayback(); }
        audio::PortResult Write(audio::PcmView input, uint32_t timeout_ms) override {
            return owner_.Write(input, timeout_ms);
        }
    private:
        LichuangAudioCodec& owner_;
    };

    LichuangAudioCodec(SharedI2cBus& i2c_bus, Pca9557Control& pca9557,
                       CodecRuntimeConfig config = {});
    ~LichuangAudioCodec();

    LichuangAudioCodec(const LichuangAudioCodec&) = delete;
    LichuangAudioCodec& operator=(const LichuangAudioCodec&) = delete;

    audio::PortResult StartCapture();
    audio::PortResult StopCapture();
    audio::PortResult StartPlayback();
    audio::PortResult StopPlayback();
    audio::PortResult Read(audio::MutablePcmView* destination, uint32_t timeout_ms);
    audio::PortResult Write(audio::PcmView input, uint32_t timeout_ms);
    audio::PortResult SetOutputVolume(int volume);
    audio::PortResult Shutdown();

    [[nodiscard]] CaptureEndpoint& capture() { return capture_endpoint_; }
    [[nodiscard]] PlaybackEndpoint& playback() { return playback_endpoint_; }

    [[nodiscard]] bool capture_started() const { return capture_started_.load(); }
    [[nodiscard]] bool playback_started() const { return playback_started_.load(); }

private:
    struct FixedI2sDataInterface final {
        audio_codec_data_if_t base{};
        const audio_codec_data_if_t* delegate = nullptr;
        bool opened = false;
    };

    static constexpr size_t kPlaybackMonoChunkSamples = 240;
    static int FixedDataOpen(const audio_codec_data_if_t* data_if, void* config, int config_size);
    static bool FixedDataIsOpen(const audio_codec_data_if_t* data_if);
    static int FixedDataEnable(
        const audio_codec_data_if_t* data_if, esp_codec_dev_type_t device_type, bool enable);
    static int FixedDataSetFormat(
        const audio_codec_data_if_t* data_if,
        esp_codec_dev_type_t device_type,
        esp_codec_dev_sample_info_t* format);
    static int FixedDataRead(const audio_codec_data_if_t* data_if, uint8_t* data, int size);
    static int FixedDataWrite(const audio_codec_data_if_t* data_if, uint8_t* data, int size);
    static int FixedDataClose(const audio_codec_data_if_t* data_if);

    audio::PortResult EnsureHardwareInitialized();
    void ReleaseHardware();

    SharedI2cBus& i2c_bus_;
    Pca9557Control& pca9557_;
    CodecRuntimeConfig config_;
    std::mutex lifecycle_mutex_;
    std::atomic<bool> capture_started_{false};
    std::atomic<bool> playback_started_{false};
    CaptureEndpoint capture_endpoint_;
    PlaybackEndpoint playback_endpoint_;
    // 10 ms at 24 kHz. Playback has one task owner, so this avoids heap work
    // while expanding mono samples into the board's stereo I2S frame.
    std::array<int16_t, kPlaybackMonoChunkSamples * 2> playback_stereo_buffer_{};

    i2s_chan_handle_t tx_channel_ = nullptr;
    i2s_chan_handle_t rx_channel_ = nullptr;
    FixedI2sDataInterface fixed_data_interface_{};
    const audio_codec_data_if_t* data_interface_ = nullptr;
    const audio_codec_data_if_t* i2s_data_delegate_ = nullptr;
    const audio_codec_ctrl_if_t* output_control_interface_ = nullptr;
    const audio_codec_if_t* output_codec_interface_ = nullptr;
    const audio_codec_ctrl_if_t* input_control_interface_ = nullptr;
    const audio_codec_if_t* input_codec_interface_ = nullptr;
    const audio_codec_gpio_if_t* gpio_interface_ = nullptr;
    esp_codec_dev_handle_t output_device_ = nullptr;
    esp_codec_dev_handle_t input_device_ = nullptr;
};

}  // namespace rva::board::lichuang_s3
