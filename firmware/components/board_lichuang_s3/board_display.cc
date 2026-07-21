#include "board_lichuang_s3/board_display.h"

#include <driver/gpio.h>
#include <driver/spi_master.h>
#include <esp_lcd_panel_vendor.h>
#include <esp_lcd_touch_ft5x06.h>
#include <esp_log.h>

#include "board_lichuang_s3/audio_config.h"
#include "board_lichuang_s3/display_config.h"

namespace rva::board::lichuang_s3 {
namespace {

constexpr char kTag[] = "rva-display";

esp_err_t RecordFirst(esp_err_t first, esp_err_t next) {
    return first == ESP_OK ? next : first;
}

}  // namespace

LichuangDisplay::LichuangDisplay(SharedI2cBus& i2c_bus,
                                 Pca9557Control& pca9557)
    : i2c_bus_(i2c_bus), pca9557_(pca9557) {}

LichuangDisplay::~LichuangDisplay() {
    Stop();
}

esp_err_t LichuangDisplay::Start() {
    if (started()) {
        return ESP_OK;
    }
    if (!i2c_bus_.started() || !pca9557_.started()) {
        return ESP_ERR_INVALID_STATE;
    }

    const gpio_config_t backlight_config = {
        .pin_bit_mask = 1ULL << DisplayConfig::kBacklightGpio,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    esp_err_t result = gpio_config(&backlight_config);
    if (result != ESP_OK) {
        return result;
    }
    backlight_configured_ = true;
    SetBacklight(false);

    spi_bus_config_t spi_config = {};
    spi_config.mosi_io_num = DisplayConfig::kSpiMosiGpio;
    spi_config.miso_io_num = GPIO_NUM_NC;
    spi_config.sclk_io_num = DisplayConfig::kSpiClockGpio;
    spi_config.quadwp_io_num = GPIO_NUM_NC;
    spi_config.quadhd_io_num = GPIO_NUM_NC;
    spi_config.max_transfer_sz =
        DisplayConfig::kWidth * DisplayConfig::kHeight * sizeof(uint16_t);
    result = spi_bus_initialize(SPI3_HOST, &spi_config, SPI_DMA_CH_AUTO);
    if (result != ESP_OK) {
        Stop();
        return result;
    }
    spi_started_ = true;

    esp_lcd_panel_io_spi_config_t panel_io_config = {};
    panel_io_config.cs_gpio_num = GPIO_NUM_NC;
    panel_io_config.dc_gpio_num = DisplayConfig::kDisplayDcGpio;
    panel_io_config.spi_mode = DisplayConfig::kSpiMode;
    panel_io_config.pclk_hz = DisplayConfig::kPixelClockHz;
    panel_io_config.trans_queue_depth = DisplayConfig::kTransactionQueueDepth;
    panel_io_config.lcd_cmd_bits = 8;
    panel_io_config.lcd_param_bits = 8;
    result = esp_lcd_new_panel_io_spi(SPI3_HOST, &panel_io_config, &panel_io_);
    if (result != ESP_OK) {
        Stop();
        return result;
    }

    esp_lcd_panel_dev_config_t panel_config = {};
    panel_config.reset_gpio_num = GPIO_NUM_NC;
    panel_config.rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB;
    panel_config.bits_per_pixel = 16;
    result = esp_lcd_new_panel_st7789(panel_io_, &panel_config, &panel_);
    if (result != ESP_OK) {
        Stop();
        return result;
    }
    if ((result = esp_lcd_panel_reset(panel_)) != ESP_OK ||
        (result = pca9557_.SetOutputState(DisplayConfig::kPca9557DisplayResetBit, false)) != ESP_OK ||
        (result = esp_lcd_panel_init(panel_)) != ESP_OK ||
        (result = esp_lcd_panel_invert_color(panel_, true)) != ESP_OK ||
        (result = esp_lcd_panel_swap_xy(panel_, DisplayConfig::kSwapXy)) != ESP_OK ||
        (result = esp_lcd_panel_mirror(
             panel_, DisplayConfig::kMirrorX, DisplayConfig::kMirrorY)) != ESP_OK ||
        (result = esp_lcd_panel_disp_on_off(panel_, true)) != ESP_OK) {
        Stop();
        return result;
    }

    esp_lcd_panel_io_i2c_config_t touch_io_config = {};
    touch_io_config.dev_addr = ESP_LCD_TOUCH_IO_I2C_FT5x06_ADDRESS;
    touch_io_config.control_phase_bytes = 1;
    touch_io_config.dc_bit_offset = 0;
    touch_io_config.lcd_cmd_bits = 8;
    touch_io_config.flags.disable_control_phase = true;
    touch_io_config.scl_speed_hz = AudioConfig::kI2cFrequencyHz;
    result = esp_lcd_new_panel_io_i2c(i2c_bus_.handle(), &touch_io_config, &touch_io_);
    if (result != ESP_OK) {
        ESP_LOGW(kTag, "Touch I2C unavailable; continuing display-only: %s",
                 esp_err_to_name(result));
        return SetBacklight(true);
    }

    const esp_lcd_touch_config_t touch_config = {
        .x_max = DisplayConfig::kHeight,
        .y_max = DisplayConfig::kWidth,
        .rst_gpio_num = GPIO_NUM_NC,
        .int_gpio_num = GPIO_NUM_NC,
        .levels = {.reset = 0, .interrupt = 0},
        .flags = {.swap_xy = true, .mirror_x = true, .mirror_y = false},
        .process_coordinates = nullptr,
        .interrupt_callback = nullptr,
        .user_data = nullptr,
        .driver_data = nullptr,
    };
    result = esp_lcd_touch_new_i2c_ft5x06(touch_io_, &touch_config, &touch_);
    if (result != ESP_OK) {
        ESP_LOGW(kTag, "Touch controller unavailable; continuing display-only: %s",
                 esp_err_to_name(result));
        esp_lcd_panel_io_del(touch_io_);
        touch_io_ = nullptr;
        return SetBacklight(true);
    }
    return SetBacklight(true);
}

esp_err_t LichuangDisplay::SetBacklight(bool enabled) {
    if (!backlight_configured_) {
        return ESP_ERR_INVALID_STATE;
    }
    const int active_level = DisplayConfig::kBacklightActiveLow ? 0 : 1;
    return gpio_set_level(
        static_cast<gpio_num_t>(DisplayConfig::kBacklightGpio),
        enabled ? active_level : 1 - active_level);
}

esp_err_t LichuangDisplay::Stop() {
    esp_err_t first_error = ESP_OK;
    if (backlight_configured_) {
        first_error = RecordFirst(first_error, SetBacklight(false));
    }
    if (touch_ != nullptr) {
        first_error = RecordFirst(first_error, esp_lcd_touch_del(touch_));
        touch_ = nullptr;
    }
    if (touch_io_ != nullptr) {
        first_error = RecordFirst(first_error, esp_lcd_panel_io_del(touch_io_));
        touch_io_ = nullptr;
    }
    if (panel_ != nullptr) {
        first_error = RecordFirst(first_error, esp_lcd_panel_disp_on_off(panel_, false));
        first_error = RecordFirst(first_error, esp_lcd_panel_del(panel_));
        panel_ = nullptr;
    }
    if (panel_io_ != nullptr) {
        first_error = RecordFirst(first_error, esp_lcd_panel_io_del(panel_io_));
        panel_io_ = nullptr;
    }
    if (spi_started_) {
        const esp_err_t result = spi_bus_free(SPI3_HOST);
        first_error = RecordFirst(first_error, result);
        if (result == ESP_OK) {
            spi_started_ = false;
        }
    }
    if (backlight_configured_) {
        first_error = RecordFirst(
            first_error,
            gpio_reset_pin(static_cast<gpio_num_t>(DisplayConfig::kBacklightGpio)));
        backlight_configured_ = false;
    }
    return first_error;
}

}  // namespace rva::board::lichuang_s3
