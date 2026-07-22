#pragma once

#include <cstddef>
#include <cstdint>

#include <lvgl.h>

namespace rva::ui {

// Loads the relative-pointer CBIN format produced by 78/xiaozhi-fonts.
// The backing bytes must outlive the returned font.
lv_font_t* CreateCbinFont(const uint8_t* data, size_t size);
void DestroyCbinFont(lv_font_t* font);

}  // namespace rva::ui
