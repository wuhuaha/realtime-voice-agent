#pragma once

#include <esp_err.h>
#include <esp_lcd_panel_io.h>
#include <esp_lcd_panel_ops.h>
#include <esp_lcd_touch.h>

#include "board_lichuang_s3/board_audio_control.h"

namespace rva::board::lichuang_s3 {

// Owns SPI3, ST7789, optional FT5x06 and the active-low backlight GPIO. The shared
// I2C/PCA9557 owners must outlive this object. LVGL bindings must be removed
// before Stop() deletes panel and touch handles.
class LichuangDisplay final {
public:
    LichuangDisplay(SharedI2cBus& i2c_bus, Pca9557Control& pca9557);
    ~LichuangDisplay();

    LichuangDisplay(const LichuangDisplay&) = delete;
    LichuangDisplay& operator=(const LichuangDisplay&) = delete;

    esp_err_t Start();
    esp_err_t SetBacklight(bool enabled);
    esp_err_t Stop();

    [[nodiscard]] bool started() const { return panel_ != nullptr; }
    [[nodiscard]] esp_lcd_panel_io_handle_t panel_io() const { return panel_io_; }
    [[nodiscard]] esp_lcd_panel_handle_t panel() const { return panel_; }
    [[nodiscard]] esp_lcd_touch_handle_t touch() const { return touch_; }

private:
    SharedI2cBus& i2c_bus_;
    Pca9557Control& pca9557_;
    esp_lcd_panel_io_handle_t panel_io_ = nullptr;
    esp_lcd_panel_handle_t panel_ = nullptr;
    esp_lcd_panel_io_handle_t touch_io_ = nullptr;
    esp_lcd_touch_handle_t touch_ = nullptr;
    bool spi_started_ = false;
    bool backlight_configured_ = false;
};

}  // namespace rva::board::lichuang_s3
