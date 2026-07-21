#include "native_runtime/opus_codec.h"

#include <limits>

#include "decoder/esp_audio_dec.h"
#include "decoder/impl/esp_opus_dec.h"
#include "encoder/esp_audio_enc.h"
#include "encoder/impl/esp_opus_enc.h"

namespace rva::runtime {

OpusCodec::~OpusCodec() {
    Stop();
}

bool OpusCodec::Start() {
    if (encoder_ != nullptr || decoder_ != nullptr) return false;
    esp_opus_enc_config_t encoder_config = ESP_OPUS_ENC_CONFIG_DEFAULT();
    encoder_config.sample_rate = 16000;
    encoder_config.channel = 1;
    encoder_config.bits_per_sample = 16;
    encoder_config.bitrate = 24000;
    encoder_config.frame_duration = ESP_OPUS_ENC_FRAME_DURATION_60_MS;
    encoder_config.application_mode = ESP_OPUS_ENC_APPLICATION_VOIP;
    encoder_config.complexity = 0;
    encoder_config.enable_fec = false;
    encoder_config.enable_dtx = true;
    encoder_config.enable_vbr = true;
    if (esp_opus_enc_open(&encoder_config, sizeof(encoder_config), &encoder_) != ESP_AUDIO_ERR_OK ||
        esp_opus_enc_get_frame_size(encoder_, &encoder_input_bytes_, &encoder_output_bytes_) != ESP_AUDIO_ERR_OK ||
        encoder_input_bytes_ != 960 * static_cast<int>(sizeof(int16_t))) {
        Stop();
        return false;
    }
    esp_opus_dec_cfg_t decoder_config = ESP_OPUS_DEC_CONFIG_DEFAULT();
    decoder_config.sample_rate = 16000;
    decoder_config.channel = 1;
    decoder_config.frame_duration = ESP_OPUS_DEC_FRAME_DURATION_60_MS;
    decoder_config.self_delimited = false;
    if (esp_opus_dec_open(&decoder_config, sizeof(decoder_config), &decoder_) != ESP_AUDIO_ERR_OK) {
        Stop();
        return false;
    }
    return true;
}

void OpusCodec::Stop() {
    if (encoder_ != nullptr) {
        esp_opus_enc_close(encoder_);
        encoder_ = nullptr;
    }
    if (decoder_ != nullptr) {
        esp_opus_dec_close(decoder_);
        decoder_ = nullptr;
    }
}

bool OpusCodec::Encode60Ms(
    const int16_t* pcm,
    size_t samples,
    uint8_t* output,
    size_t capacity,
    size_t* output_size) {
    if (encoder_ == nullptr || pcm == nullptr || samples != 960 || output == nullptr || output_size == nullptr ||
        capacity < static_cast<size_t>(encoder_output_bytes_) || capacity > std::numeric_limits<uint32_t>::max()) {
        return false;
    }
    esp_audio_enc_in_frame_t input{
        .buffer = reinterpret_cast<uint8_t*>(const_cast<int16_t*>(pcm)),
        .len = static_cast<uint32_t>(samples * sizeof(int16_t)),
    };
    esp_audio_enc_out_frame_t encoded{
        .buffer = output,
        .len = static_cast<uint32_t>(capacity),
        .encoded_bytes = 0,
        .pts = 0,
    };
    if (esp_opus_enc_process(encoder_, &input, &encoded) != ESP_AUDIO_ERR_OK ||
        encoded.encoded_bytes > capacity) {
        return false;
    }
    *output_size = encoded.encoded_bytes;
    return true;
}

bool OpusCodec::Decode60Ms(
    const uint8_t* opus,
    size_t size,
    int16_t* output,
    size_t capacity_samples,
    size_t* samples) {
    return Decode(opus, size, false, output, capacity_samples, samples);
}

bool OpusCodec::DecodePlc60Ms(
    int16_t* output,
    size_t capacity_samples,
    size_t* samples) {
    return Decode(nullptr, 0, true, output, capacity_samples, samples);
}

bool OpusCodec::Decode(
    const uint8_t* opus,
    size_t size,
    bool recover,
    int16_t* output,
    size_t capacity_samples,
    size_t* samples) {
    uint8_t empty = 0;
    if (decoder_ == nullptr || (!recover && (opus == nullptr || size == 0)) ||
        size > std::numeric_limits<uint32_t>::max() ||
        output == nullptr || capacity_samples < 960 || samples == nullptr) {
        return false;
    }
    esp_audio_dec_in_raw_t raw{
        .buffer = recover ? &empty : const_cast<uint8_t*>(opus),
        .len = static_cast<uint32_t>(size),
        .consumed = 0,
        .frame_recover = recover ? ESP_AUDIO_DEC_RECOVERY_PLC : ESP_AUDIO_DEC_RECOVERY_NONE,
    };
    esp_audio_dec_out_frame_t frame{
        .buffer = reinterpret_cast<uint8_t*>(output),
        .len = static_cast<uint32_t>(capacity_samples * sizeof(int16_t)),
        .needed_size = 0,
        .decoded_size = 0,
    };
    esp_audio_dec_info_t information{};
    if (esp_opus_dec_decode(decoder_, &raw, &frame, &information) != ESP_AUDIO_ERR_OK ||
        frame.decoded_size > capacity_samples * sizeof(int16_t) || frame.decoded_size % sizeof(int16_t) != 0) {
        return false;
    }
    *samples = frame.decoded_size / sizeof(int16_t);
    return true;
}

}  // namespace rva::runtime
