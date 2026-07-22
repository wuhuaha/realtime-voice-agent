#pragma once

#include <esp_err.h>
#include <esp_partition.h>
#include <lvgl.h>

namespace rva::ui {

// Validates and transiently maps the immutable assets partition, then owns the
// LVGL binary-font descriptor until the UI is stopped.
class FontAssets final {
public:
    FontAssets() = default;
    ~FontAssets();

    FontAssets(const FontAssets&) = delete;
    FontAssets& operator=(const FontAssets&) = delete;

    esp_err_t Initialize();
    void Deinitialize();
    const lv_font_t* text_font() const { return text_font_; }
    bool ready() const { return text_font_ != nullptr; }

private:
    lv_font_t* text_font_ = nullptr;
    esp_partition_mmap_handle_t mapping_ = 0;
};

}  // namespace rva::ui
