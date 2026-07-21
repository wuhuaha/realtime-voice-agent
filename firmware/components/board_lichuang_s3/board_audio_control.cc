#include "board_lichuang_s3/board_audio_control.h"

#include <esp_log.h>

#include "board_lichuang_s3/audio_config.h"

namespace rva::board::lichuang_s3 {
namespace {

constexpr char kTag[] = "lichuang_audio";
constexpr uint8_t kPca9557OutputPortRegister = 0x01;
constexpr uint8_t kPca9557ConfigurationRegister = 0x03;
constexpr uint8_t kVerifiedInitialOutput = 0x03;
constexpr uint8_t kVerifiedDirection = 0xF8;
constexpr uint8_t kPowerAmplifierMask =
    static_cast<uint8_t>(1U << AudioConfig::kPca9557PowerAmplifierBit);
constexpr uint8_t kSafeInitialOutput =
    static_cast<uint8_t>(kVerifiedInitialOutput & static_cast<uint8_t>(~kPowerAmplifierMask));
constexpr int kTransferTimeoutMs = 100;

}  // namespace

SharedI2cBus::~SharedI2cBus() {
    const esp_err_t result = Stop();
    if (result != ESP_OK) {
        ESP_LOGE(kTag, "I2C bus cleanup failed: %s", esp_err_to_name(result));
    }
}

esp_err_t SharedI2cBus::Start() {
    if (handle_ != nullptr) {
        return ESP_OK;
    }

    const i2c_master_bus_config_t config = {
        .i2c_port = static_cast<i2c_port_num_t>(AudioConfig::kI2cPort),
        .sda_io_num = static_cast<gpio_num_t>(AudioConfig::kI2cSdaGpio),
        .scl_io_num = static_cast<gpio_num_t>(AudioConfig::kI2cSclGpio),
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .intr_priority = 0,
        .trans_queue_depth = 0,
        .flags = {.enable_internal_pullup = true, .allow_pd = false},
    };
    return i2c_new_master_bus(&config, &handle_);
}

esp_err_t SharedI2cBus::Stop() {
    if (handle_ == nullptr) {
        return ESP_OK;
    }
    const esp_err_t result = i2c_del_master_bus(handle_);
    if (result == ESP_OK) {
        handle_ = nullptr;
    }
    return result;
}

Pca9557Control::~Pca9557Control() {
    const esp_err_t result = Stop();
    if (result != ESP_OK) {
        ESP_LOGE(kTag, "PA control cleanup failed: %s", esp_err_to_name(result));
    }
}

esp_err_t Pca9557Control::Start(i2c_master_bus_handle_t bus) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (device_ != nullptr) {
        return ESP_OK;
    }
    if (bus == nullptr) {
        return ESP_ERR_INVALID_ARG;
    }

    const i2c_device_config_t config = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = AudioConfig::kPca9557Address7Bit,
        .scl_speed_hz = AudioConfig::kI2cFrequencyHz,
        .scl_wait_us = 0,
        .flags = {.disable_ack_check = false},
    };
    esp_err_t result = i2c_master_bus_add_device(bus, &config, &device_);
    if (result != ESP_OK) {
        return result;
    }

    // Keep PA_EN low while changing the expander direction to avoid a startup pop.
    result = WriteRegister(kPca9557OutputPortRegister, kSafeInitialOutput);
    if (result == ESP_OK) {
        result = WriteRegister(kPca9557ConfigurationRegister, kVerifiedDirection);
    }
    if (result == ESP_OK) {
        amplifier_enabled_ = false;
    }
    if (result != ESP_OK) {
        const esp_err_t remove_result = i2c_master_bus_rm_device(device_);
        if (remove_result != ESP_OK) {
            ESP_LOGE(kTag, "PCA9557 rollback failed: %s", esp_err_to_name(remove_result));
        } else {
            device_ = nullptr;
        }
        amplifier_enabled_ = false;
    }
    return result;
}

esp_err_t Pca9557Control::SetPowerAmplifierEnabled(bool enabled) {
    std::lock_guard<std::mutex> lock(mutex_);
    const esp_err_t result = SetOutputStateLocked(
        AudioConfig::kPca9557PowerAmplifierBit, enabled);
    if (result == ESP_OK) {
        amplifier_enabled_ = enabled;
    }
    return result;
}

esp_err_t Pca9557Control::SetOutputState(uint8_t bit, bool high) {
    std::lock_guard<std::mutex> lock(mutex_);
    return SetOutputStateLocked(bit, high);
}

esp_err_t Pca9557Control::SetOutputStateLocked(uint8_t bit, bool high) {
    if (device_ == nullptr) {
        return ESP_ERR_INVALID_STATE;
    }
    if (bit >= 8) {
        return ESP_ERR_INVALID_ARG;
    }
    uint8_t output = 0;
    esp_err_t result = ReadRegister(kPca9557OutputPortRegister, &output);
    if (result != ESP_OK) {
        return result;
    }

    const uint8_t mask = static_cast<uint8_t>(1U << bit);
    output = high ? static_cast<uint8_t>(output | mask)
                  : static_cast<uint8_t>(output & static_cast<uint8_t>(~mask));
    result = WriteRegister(kPca9557OutputPortRegister, output);
    return result;
}

esp_err_t Pca9557Control::Stop() {
    std::lock_guard<std::mutex> lock(mutex_);
    if (device_ == nullptr) {
        return ESP_OK;
    }

    esp_err_t first_error = SetOutputStateLocked(
        AudioConfig::kPca9557PowerAmplifierBit, false);
    const esp_err_t remove_result = i2c_master_bus_rm_device(device_);
    if (first_error == ESP_OK) {
        first_error = remove_result;
    }
    if (remove_result == ESP_OK) {
        device_ = nullptr;
        amplifier_enabled_ = false;
    }
    return first_error;
}

esp_err_t Pca9557Control::WriteRegister(uint8_t reg, uint8_t value) {
    const uint8_t payload[] = {reg, value};
    return i2c_master_transmit(device_, payload, sizeof(payload), kTransferTimeoutMs);
}

esp_err_t Pca9557Control::ReadRegister(uint8_t reg, uint8_t* value) {
    if (value == nullptr) {
        return ESP_ERR_INVALID_ARG;
    }
    return i2c_master_transmit_receive(
        device_, &reg, sizeof(reg), value, sizeof(*value), kTransferTimeoutMs);
}

}  // namespace rva::board::lichuang_s3
