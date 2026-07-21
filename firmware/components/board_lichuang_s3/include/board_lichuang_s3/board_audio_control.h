#pragma once

#include <driver/i2c_master.h>
#include <esp_err.h>
#include <mutex>

namespace rva::board::lichuang_s3 {

// Owns the shared board I2C bus. Codec devices must be removed before Stop().
class SharedI2cBus final {
public:
    SharedI2cBus() = default;
    ~SharedI2cBus();

    SharedI2cBus(const SharedI2cBus&) = delete;
    SharedI2cBus& operator=(const SharedI2cBus&) = delete;

    esp_err_t Start();
    esp_err_t Stop();

    [[nodiscard]] i2c_master_bus_handle_t handle() const { return handle_; }
    [[nodiscard]] bool started() const { return handle_ != nullptr; }

private:
    i2c_master_bus_handle_t handle_ = nullptr;
};

// Owns only the PCA9557 device handle and PA_EN bit. It does not own the bus.
class Pca9557Control final {
public:
    Pca9557Control() = default;
    ~Pca9557Control();

    Pca9557Control(const Pca9557Control&) = delete;
    Pca9557Control& operator=(const Pca9557Control&) = delete;

    esp_err_t Start(i2c_master_bus_handle_t bus);
    esp_err_t SetPowerAmplifierEnabled(bool enabled);
    esp_err_t SetOutputState(uint8_t bit, bool high);
    esp_err_t Stop();

    [[nodiscard]] bool started() const { return device_ != nullptr; }
    [[nodiscard]] bool power_amplifier_enabled() const { return amplifier_enabled_; }

private:
    esp_err_t WriteRegister(uint8_t reg, uint8_t value);
    esp_err_t ReadRegister(uint8_t reg, uint8_t* value);
    esp_err_t SetOutputStateLocked(uint8_t bit, bool high);

    i2c_master_dev_handle_t device_ = nullptr;
    bool amplifier_enabled_ = false;
    std::mutex mutex_;
};

}  // namespace rva::board::lichuang_s3
