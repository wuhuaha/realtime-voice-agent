#include "ui_font_assets/cbin_font.h"

#include <cstdint>
#include <cstring>
#include <limits>

namespace rva::ui {
namespace {

constexpr size_t kPackedCmapBytes = 20;

bool Contains(size_t size, uintptr_t offset, size_t bytes) {
    return offset <= size && bytes <= size - offset;
}

template <typename T>
T* CopyObject(const uint8_t* data, size_t size, uintptr_t offset) {
    if (!Contains(size, offset, sizeof(T))) return nullptr;
    auto* output = static_cast<T*>(lv_malloc(sizeof(T)));
    if (output != nullptr) std::memcpy(output, data + offset, sizeof(T));
    return output;
}

template <typename T>
T ReadUnaligned(const uint8_t* data) {
    T value{};
    std::memcpy(&value, data, sizeof(value));
    return value;
}

const void* Resolve(const uint8_t* base, size_t size, uintptr_t offset) {
    if (offset == 0) return nullptr;
    return Contains(size, offset, 1) ? base + offset : nullptr;
}

}  // namespace

lv_font_t* CreateCbinFont(const uint8_t* data, size_t size) {
    if (data == nullptr || size < sizeof(lv_font_t)) return nullptr;

    lv_font_t* font = CopyObject<lv_font_t>(data, size, 0);
    if (font == nullptr) return nullptr;
    font->get_glyph_dsc = lv_font_get_glyph_dsc_fmt_txt;
    font->get_glyph_bitmap = lv_font_get_bitmap_fmt_txt;

    const uintptr_t descriptor_offset = reinterpret_cast<uintptr_t>(font->dsc);
    auto* descriptor = CopyObject<lv_font_fmt_txt_dsc_t>(data, size, descriptor_offset);
    if (descriptor == nullptr) {
        lv_free(font);
        return nullptr;
    }
    font->dsc = descriptor;
    const uint8_t* descriptor_base = data + descriptor_offset;
    const size_t descriptor_size = size - descriptor_offset;
    const uintptr_t cmaps_offset = reinterpret_cast<uintptr_t>(descriptor->cmaps);
    const uintptr_t kerning_offset = reinterpret_cast<uintptr_t>(descriptor->kern_dsc);
    descriptor->cmaps = nullptr;
    descriptor->kern_dsc = nullptr;

    descriptor->glyph_bitmap = static_cast<const uint8_t*>(Resolve(
        descriptor_base, descriptor_size,
        reinterpret_cast<uintptr_t>(descriptor->glyph_bitmap)));
    descriptor->glyph_dsc = static_cast<const lv_font_fmt_txt_glyph_dsc_t*>(Resolve(
        descriptor_base, descriptor_size,
        reinterpret_cast<uintptr_t>(descriptor->glyph_dsc)));
    if (descriptor->glyph_bitmap == nullptr || descriptor->glyph_dsc == nullptr) {
        DestroyCbinFont(font);
        return nullptr;
    }

    if (descriptor->cmap_num > 0) {
        if (descriptor->cmap_num > std::numeric_limits<size_t>::max() / kPackedCmapBytes ||
            !Contains(descriptor_size, cmaps_offset,
                      static_cast<size_t>(descriptor->cmap_num) * kPackedCmapBytes)) {
            DestroyCbinFont(font);
            return nullptr;
        }
        auto* cmaps = static_cast<lv_font_fmt_txt_cmap_t*>(
            lv_malloc(sizeof(lv_font_fmt_txt_cmap_t) * descriptor->cmap_num));
        if (cmaps == nullptr) {
            DestroyCbinFont(font);
            return nullptr;
        }
        std::memset(cmaps, 0, sizeof(lv_font_fmt_txt_cmap_t) * descriptor->cmap_num);
        descriptor->cmaps = cmaps;
        const uint8_t* packed = descriptor_base + cmaps_offset;
        for (uint32_t index = 0; index < descriptor->cmap_num; ++index, packed += kPackedCmapBytes) {
            auto& cmap = cmaps[index];
            cmap.range_start = ReadUnaligned<uint32_t>(packed);
            cmap.range_length = ReadUnaligned<uint16_t>(packed + 4);
            cmap.glyph_id_start = ReadUnaligned<uint16_t>(packed + 6);
            const uintptr_t unicode_offset = ReadUnaligned<uint32_t>(packed + 8);
            const uintptr_t glyph_offset = ReadUnaligned<uint32_t>(packed + 12);
            cmap.list_length = ReadUnaligned<uint16_t>(packed + 16);
            cmap.type = static_cast<lv_font_fmt_txt_cmap_type_t>(packed[18]);
            cmap.unicode_list = static_cast<const uint16_t*>(
                Resolve(descriptor_base + cmaps_offset, descriptor_size - cmaps_offset,
                        unicode_offset));
            cmap.glyph_id_ofs_list = Resolve(
                descriptor_base + cmaps_offset, descriptor_size - cmaps_offset, glyph_offset);
            if ((unicode_offset != 0 && cmap.unicode_list == nullptr) ||
                (glyph_offset != 0 && cmap.glyph_id_ofs_list == nullptr)) {
                DestroyCbinFont(font);
                return nullptr;
            }
        }
    } else {
        descriptor->cmaps = nullptr;
    }

    if (kerning_offset != 0) {
        if (descriptor->kern_classes == 1) {
            auto* kerning = CopyObject<lv_font_fmt_txt_kern_classes_t>(
                descriptor_base, descriptor_size, kerning_offset);
            if (kerning == nullptr) {
                DestroyCbinFont(font);
                return nullptr;
            }
            const uint8_t* kerning_base = descriptor_base + kerning_offset;
            const size_t kerning_size = descriptor_size - kerning_offset;
            kerning->class_pair_values = static_cast<const int8_t*>(Resolve(
                kerning_base, kerning_size,
                reinterpret_cast<uintptr_t>(kerning->class_pair_values)));
            kerning->left_class_mapping = static_cast<const uint8_t*>(Resolve(
                kerning_base, kerning_size,
                reinterpret_cast<uintptr_t>(kerning->left_class_mapping)));
            kerning->right_class_mapping = static_cast<const uint8_t*>(Resolve(
                kerning_base, kerning_size,
                reinterpret_cast<uintptr_t>(kerning->right_class_mapping)));
            descriptor->kern_dsc = kerning;
        } else {
            auto* kerning = CopyObject<lv_font_fmt_txt_kern_pair_t>(
                descriptor_base, descriptor_size, kerning_offset);
            if (kerning == nullptr) {
                DestroyCbinFont(font);
                return nullptr;
            }
            const uint8_t* kerning_base = descriptor_base + kerning_offset;
            const size_t kerning_size = descriptor_size - kerning_offset;
            kerning->glyph_ids = Resolve(
                kerning_base, kerning_size,
                reinterpret_cast<uintptr_t>(kerning->glyph_ids));
            kerning->values = static_cast<const int8_t*>(Resolve(
                kerning_base, kerning_size,
                reinterpret_cast<uintptr_t>(kerning->values)));
            descriptor->kern_dsc = kerning;
        }
    }
    return font;
}

void DestroyCbinFont(lv_font_t* font) {
    if (font == nullptr) return;
    auto* descriptor = static_cast<lv_font_fmt_txt_dsc_t*>(const_cast<void*>(font->dsc));
    if (descriptor != nullptr) {
        lv_free(const_cast<lv_font_fmt_txt_cmap_t*>(descriptor->cmaps));
        lv_free(const_cast<void*>(descriptor->kern_dsc));
        lv_free(descriptor);
    }
    lv_free(font);
}

}  // namespace rva::ui
