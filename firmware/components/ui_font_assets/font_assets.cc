#include "ui_font_assets/font_assets.h"

#include <cstddef>
#include <cstdint>
#include <cstring>

#include <esp_log.h>
#include <esp_partition.h>
#include <mbedtls/sha256.h>

#include "ui_font_assets/cbin_font.h"

namespace rva::ui {
namespace {

constexpr char kTag[] = "rva_font_assets";
constexpr char kPartitionLabel[] = "font_assets";
constexpr uint8_t kMagic[8] = {'R', 'V', 'A', 'F', 'N', 'T', '1', '\0'};
constexpr uint8_t kExpectedSourceId[16] = {'q', 'w', 'e', 'n', '2', '0', '-', '4',
                                           '-', 'v', '1', '.', '6', '.', '0', '\0'};
constexpr uint8_t kExpectedFontSha256[32] = {
    0x60, 0x14, 0x22, 0xde, 0x3a, 0x49, 0xc0, 0x52, 0x65, 0xed, 0x85,
    0x3c, 0x80, 0x54, 0xb7, 0x3b, 0x53, 0x27, 0x29, 0xe6, 0x67, 0xa6,
    0xd6, 0x3f, 0x34, 0xbb, 0x72, 0xea, 0xb1, 0x93, 0x53, 0x45,
};
constexpr uint16_t kFormatVersion = 1;
constexpr size_t kMaxFontBytes = 6U * 1024U * 1024U;

bool GlyphUsable(const lv_font_t* font, uint32_t codepoint, const char* name) {
    lv_font_glyph_dsc_t glyph{};
    const bool found = lv_font_get_glyph_dsc(font, &glyph, codepoint, 0);
    if (!found || glyph.resolved_font != font ||
        glyph.adv_w == 0 || glyph.box_w == 0 || glyph.box_h == 0) {
        ESP_LOGW(kTag,
                 "font glyph invalid: %s U+%04lX found=%d resolved=%d adv=%u box=%ux%u gid=%lu",
                 name, static_cast<unsigned long>(codepoint), found,
                 glyph.resolved_font == font, static_cast<unsigned>(glyph.adv_w),
                 static_cast<unsigned>(glyph.box_w), static_cast<unsigned>(glyph.box_h),
                 static_cast<unsigned long>(glyph.gid.index));
        return false;
    }
    if (font->static_bitmap != 0 && lv_font_get_glyph_static_bitmap(&glyph) == nullptr) {
        ESP_LOGW(kTag, "font glyph bitmap unavailable: %s U+%04lX", name,
                 static_cast<unsigned long>(codepoint));
        return false;
    }
    return true;
}

struct __attribute__((packed)) FontAssetHeader final {
    uint8_t magic[8];
    uint16_t version;
    uint16_t header_size;
    uint32_t font_size;
    uint8_t font_sha256[32];
    uint8_t source_id[16];
};

static_assert(sizeof(FontAssetHeader) == 64);

}  // namespace

FontAssets::~FontAssets() {
    Deinitialize();
}

esp_err_t FontAssets::Initialize() {
    if (ready()) return ESP_OK;
    Deinitialize();

    const esp_partition_t* partition = esp_partition_find_first(
        ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_ANY, kPartitionLabel);
    if (partition == nullptr) {
        ESP_LOGW(kTag, "font assets unavailable: partition_missing");
        return ESP_ERR_NOT_FOUND;
    }

    FontAssetHeader header{};
    esp_err_t result = esp_partition_read(partition, 0, &header, sizeof(header));
    if (result != ESP_OK) {
        ESP_LOGW(kTag, "font assets unavailable: header_read_failed (%s)",
                 esp_err_to_name(result));
        return result;
    }
    if (std::memcmp(header.magic, kMagic, sizeof(kMagic)) != 0 ||
        header.version != kFormatVersion || header.header_size != sizeof(header) ||
        std::memcmp(header.source_id, kExpectedSourceId, sizeof(kExpectedSourceId)) != 0 ||
        std::memcmp(header.font_sha256, kExpectedFontSha256,
                    sizeof(kExpectedFontSha256)) != 0) {
        ESP_LOGW(kTag, "font assets unavailable: header_invalid");
        return ESP_ERR_INVALID_VERSION;
    }
    if (partition->size < sizeof(header) || header.font_size == 0U ||
        header.font_size > kMaxFontBytes ||
        header.font_size > partition->size - sizeof(header)) {
        ESP_LOGW(kTag, "font assets unavailable: size_invalid (%lu)",
                 static_cast<unsigned long>(header.font_size));
        return ESP_ERR_INVALID_SIZE;
    }

    const void* mapped = nullptr;
    const size_t mapped_size = sizeof(header) + header.font_size;
    result = esp_partition_mmap(partition, 0, mapped_size, ESP_PARTITION_MMAP_DATA,
                                &mapped, &mapping_);
    if (result != ESP_OK) {
        ESP_LOGW(kTag, "font assets unavailable: map_failed (%s)", esp_err_to_name(result));
        return result;
    }
    const auto* font_bytes = static_cast<const uint8_t*>(mapped) + sizeof(header);
    uint8_t digest[32];
    if (mbedtls_sha256(font_bytes, header.font_size, digest, 0) != 0 ||
        std::memcmp(digest, kExpectedFontSha256, sizeof(digest)) != 0) {
        ESP_LOGW(kTag, "font assets unavailable: integrity_failed");
        Deinitialize();
        return ESP_ERR_INVALID_CRC;
    }

    text_font_ = CreateCbinFont(font_bytes, header.font_size);
    if (text_font_ == nullptr) {
        ESP_LOGW(kTag, "font assets unavailable: descriptor_invalid");
        Deinitialize();
        return ESP_ERR_INVALID_RESPONSE;
    }
    // A valid container is not sufficient: reject a descriptor whose relative
    // pointers produce empty glyphs before it can blank the inherited UI font.
    if (!GlyphUsable(text_font_, 'A', "latin") ||
        !GlyphUsable(text_font_, 0x5F85, "cjk-wait") ||
        !GlyphUsable(text_font_, 0x7F51, "cjk-network")) {
        ESP_LOGW(kTag, "font assets unavailable: glyph_self_test_failed");
        Deinitialize();
        return ESP_ERR_INVALID_RESPONSE;
    }
    text_font_->fallback = &lv_font_source_han_sans_sc_16_cjk;
    ESP_LOGI(kTag, "font assets ready: %lu bytes, source=%.16s",
             static_cast<unsigned long>(header.font_size), header.source_id);
    return ESP_OK;
}

void FontAssets::Deinitialize() {
    if (text_font_ != nullptr) DestroyCbinFont(text_font_);
    text_font_ = nullptr;
    if (mapping_ != 0) {
        esp_partition_munmap(mapping_);
        mapping_ = 0;
    }
}

}  // namespace rva::ui
