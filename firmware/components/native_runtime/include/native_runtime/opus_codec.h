#pragma once

#include <cstddef>
#include <cstdint>

namespace rva::runtime {

class OpusCodec final {
public:
    ~OpusCodec();

    bool Start();
    void Stop();
    bool Encode60Ms(const int16_t* pcm, size_t samples, uint8_t* output, size_t capacity, size_t* output_size);
    bool Decode60Ms(const uint8_t* opus, size_t size, int16_t* output, size_t capacity_samples, size_t* samples);
    bool DecodePlc60Ms(int16_t* output, size_t capacity_samples, size_t* samples);

private:
    bool Decode(const uint8_t* opus, size_t size, bool recover,
                int16_t* output, size_t capacity_samples, size_t* samples);
    void* encoder_ = nullptr;
    void* decoder_ = nullptr;
    int encoder_input_bytes_ = 0;
    int encoder_output_bytes_ = 0;
};

}  // namespace rva::runtime
