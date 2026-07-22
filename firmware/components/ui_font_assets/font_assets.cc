#include "ui_font_assets/font_assets.h"

#include <cstddef>
#include <cstdint>
#include <cstring>

#include <esp_log.h>
#include <esp_partition.h>
#include <mbedtls/sha256.h>

namespace rva::ui {
namespace {

constexpr char kTag[] = "rva_font_assets";
constexpr char kPartitionLabel[] = "font_assets";
constexpr uint8_t kMagic[8] = {'R', 'V', 'A', 'F', 'N', 'T', '1', '\0'};
constexpr uint8_t kExpectedSourceId[16] = {'n', 'o', 't', 'o', 'c', 'j', 'k', '-',
                                           's', '2', '.', '0', '0', '4', '\0', '\0'};
constexpr uint8_t kExpectedFontSha256[32] = {
    0xe0, 0x16, 0x40, 0x8d, 0x52, 0x88, 0x1c, 0x4f, 0x3c, 0x71, 0x00,
    0xc5, 0x9b, 0xf6, 0xbc, 0x65, 0x74, 0xef, 0x5c, 0x03, 0x98, 0x11,
    0xca, 0xae, 0xa2, 0xdc, 0x09, 0x55, 0xa3, 0xa0, 0xa4, 0xb7,
};
constexpr uint16_t kFormatVersion = 1;
constexpr size_t kMaxFontBytes = 6U * 1024U * 1024U;

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

    // LVGL 9.5's standard binary-font parser reads through bounded MEMFS and
    // owns the resulting descriptors and glyph bitmap.
    text_font_ = lv_binfont_create_from_buffer(
        const_cast<uint8_t*>(font_bytes), header.font_size);
    if (text_font_ == nullptr) {
        ESP_LOGW(kTag, "font assets unavailable: descriptor_invalid");
        Deinitialize();
        return ESP_ERR_INVALID_RESPONSE;
    }
    esp_partition_munmap(mapping_);
    mapping_ = 0;
    text_font_->fallback = &lv_font_source_han_sans_sc_16_cjk;
    ESP_LOGI(kTag, "font assets ready: %lu bytes, source=%.16s",
             static_cast<unsigned long>(header.font_size), header.source_id);
    return ESP_OK;
}

void FontAssets::Deinitialize() {
    if (text_font_ != nullptr) lv_binfont_destroy(text_font_);
    text_font_ = nullptr;
    if (mapping_ != 0) {
        esp_partition_munmap(mapping_);
        mapping_ = 0;
    }
}

}  // namespace rva::ui
