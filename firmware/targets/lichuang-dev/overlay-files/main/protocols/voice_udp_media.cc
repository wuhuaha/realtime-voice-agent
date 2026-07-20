#include "voice_udp_media.h"

#include "board.h"

#include <algorithm>
#include <cstdlib>
#include <cstring>
#include <cmath>
#include <esp_heap_caps.h>
#include <esp_log.h>
#include <esp_timer.h>
#include <mbedtls/base64.h>

namespace {

constexpr char kTag[] = "VoiceUdp";
constexpr uint32_t kTimestampClockHz = 16000;
constexpr uint32_t kFrameDurationMs = 60;
constexpr uint32_t kTimestampSamplesPerFrame =
    kTimestampClockHz * kFrameDurationMs / 1000;
static_assert(kTimestampSamplesPerFrame == 960);

}  // namespace

VoiceUdpMedia::VoiceUdpMedia() {
    events_ = xEventGroupCreate();
    send_buffer_.reserve(voice_udp_wire::kMaxDatagramBytes);
    mbedtls_gcm_init(&uplink_gcm_);
    mbedtls_gcm_init(&downlink_gcm_);
}

VoiceUdpMedia::~VoiceUdpMedia() {
    Stop();
    mbedtls_gcm_free(&uplink_gcm_);
    mbedtls_gcm_free(&downlink_gcm_);
    if (events_ != nullptr) {
        vEventGroupDelete(events_);
    }
}

bool VoiceUdpMedia::Configure(const cJSON* udp) {
    if (!cJSON_IsObject(udp)) {
        return false;
    }
    const auto* server = cJSON_GetObjectItemCaseSensitive(udp, "server");
    const auto* port = cJSON_GetObjectItemCaseSensitive(udp, "port");
    const auto* epoch = cJSON_GetObjectItemCaseSensitive(udp, "media_epoch");
    uint32_t parsed_port = 0;
    uint32_t parsed_epoch = 0;
    if (!cJSON_IsString(server) || !ParseExactUint32(port, parsed_port) ||
        !ParseExactUint32(epoch, parsed_epoch) || parsed_port == 0 || parsed_port > 65535 ||
        parsed_epoch == 0) {
        return false;
    }
    std::array<uint8_t, kKeyBytes> uplink_key{};
    std::array<uint8_t, kKeyBytes> downlink_key{};
    if (!DecodeHex(cJSON_GetObjectItemCaseSensitive(udp, "media_id"), media_id_.data(), media_id_.size()) ||
        !DecodeBase64(cJSON_GetObjectItemCaseSensitive(udp, "uplink_key"), uplink_key.data(), uplink_key.size()) ||
        !DecodeBase64(cJSON_GetObjectItemCaseSensitive(udp, "uplink_salt"), uplink_salt_.data(), uplink_salt_.size()) ||
        !DecodeBase64(cJSON_GetObjectItemCaseSensitive(udp, "downlink_key"), downlink_key.data(), downlink_key.size()) ||
        !DecodeBase64(cJSON_GetObjectItemCaseSensitive(udp, "downlink_salt"), downlink_salt_.data(), downlink_salt_.size())) {
        return false;
    }
    if (mbedtls_gcm_setkey(&uplink_gcm_, MBEDTLS_CIPHER_ID_AES, uplink_key.data(), kKeyBytes * 8) != 0 ||
        mbedtls_gcm_setkey(&downlink_gcm_, MBEDTLS_CIPHER_ID_AES, downlink_key.data(), kKeyBytes * 8) != 0) {
        return false;
    }
    host_ = server->valuestring;
    port_ = static_cast<int>(parsed_port);
    media_epoch_ = parsed_epoch;
    send_sequence_ = 0;
    receive_highest_ = 0;
    receive_bitmap_ = 0;
    expected_audio_sequence_ = 1;
    reorder_cursor_initialized_ = false;
    received_ = 0;
    invalid_ = 0;
    replayed_ = 0;
    lost_ = 0;
    queue_dropped_ = 0;
    played_ = 0;
    concealed_ = 0;
    tx_crypto_samples_ = 0;
    rx_crypto_samples_ = 0;
    tx_crypto_us_total_ = 0;
    rx_crypto_us_total_ = 0;
    tx_crypto_us_max_ = 0;
    rx_crypto_us_max_ = 0;
    reorder_stack_min_words_ = UINT32_MAX;
    generation_.store(1, std::memory_order_release);
    ClearSlots();
    return !host_.empty() && media_epoch_ != 0;
}

bool VoiceUdpMedia::Start() {
    std::lock_guard<std::mutex> lifecycle_lock(lifecycle_mutex_);
    if (start_attempted_) {
        return ready_.load(std::memory_order_acquire);
    }
    start_attempted_ = true;
    if (events_ == nullptr || revoked_.load(std::memory_order_acquire)) {
        return false;
    }
    xEventGroupClearBits(
        events_, kProbeAckEvent | kTaskExitedEvent | kStopRequestedEvent |
                     kDataReadyEvent);
    if (revoked_.load(std::memory_order_acquire)) {
        return false;
    }
    auto network = Board::GetInstance().GetNetwork();
    udp_ = network->CreateUdp(2);
    if (udp_ == nullptr || revoked_.load(std::memory_order_acquire)) {
        RequestStop();
        return false;
    }
    udp_->OnMessage([this](const std::string& datagram) { HandleDatagram(datagram); });
    if (!udp_->Connect(host_, port_)) {
        RequestStop();
        return false;
    }
    if (revoked_.load(std::memory_order_acquire)) {
        return false;
    }
    running_.store(true, std::memory_order_release);
    TaskHandle_t reorder_task = nullptr;
    if (xTaskCreate(ReorderTask, "voice_udp_jitter", 4096, this, 4,
                    &reorder_task) != pdPASS) {
        RequestStop();
        return false;
    }
    reorder_task_.store(reorder_task, std::memory_order_release);
    for (int attempt = 1; attempt <= kProbeAttempts; ++attempt) {
        if (revoked_.load(std::memory_order_acquire)) {
            return false;
        }
        if (!SendPacket(voice_udp_wire::kProbe, nullptr, 0, 0,
                        generation_.load(std::memory_order_acquire))) {
            RequestStop();
            return false;
        }
        const EventBits_t bits = xEventGroupWaitBits(
            events_, kProbeAckEvent | kStopRequestedEvent, pdFALSE, pdFALSE,
            pdMS_TO_TICKS(kProbeAttemptWaitMs));
        if ((bits & kStopRequestedEvent) != 0 ||
            revoked_.load(std::memory_order_acquire)) {
            return false;
        }
        if ((bits & kProbeAckEvent) != 0) {
            ready_.store(true, std::memory_order_release);
            if (revoked_.load(std::memory_order_acquire)) {
                ready_.store(false, std::memory_order_release);
                return false;
            }
            ESP_LOGI(kTag, "Authenticated UDP media ready host=%s port=%d attempts=%d",
                     host_.c_str(), port_, attempt);
            return true;
        }
    }
    ESP_LOGW(kTag, "Authenticated UDP probe timed out attempts=%d", kProbeAttempts);
    RequestStop();
    return false;
}

void VoiceUdpMedia::RequestStop() {
    const bool already_revoked = revoked_.exchange(true, std::memory_order_acq_rel);
    ready_.store(false, std::memory_order_release);
    running_.store(false, std::memory_order_release);
    {
        // This short fence makes RequestStop return only after an admission
        // already committing under slots_mutex_ has completed, and prevents
        // decrypted packets from being queued after revocation.
        std::lock_guard<std::mutex> lock(slots_mutex_);
        ClearSlotsLocked();
        reorder_cursor_initialized_ = false;
    }
    if (events_ != nullptr) {
        xEventGroupSetBits(events_, kStopRequestedEvent);
    }
    if (!already_revoked) {
        ESP_LOGI(kTag, "UDP media grant revoked");
    }
}

void VoiceUdpMedia::Stop() {
    RequestStop();
    std::lock_guard<std::mutex> lifecycle_lock(lifecycle_mutex_);
    if (cleanup_complete_) {
        return;
    }
    cleanup_complete_ = true;
    // EspUdp::Disconnect joins its ingress task. No callback may retain this
    // owner after the jitter consumer begins its own shutdown.
    udp_.reset();
    const TaskHandle_t reorder_task =
        reorder_task_.load(std::memory_order_acquire);
    if (reorder_task != nullptr) {
        const EventBits_t exit_bits = xEventGroupWaitBits(
            events_, kTaskExitedEvent, pdFALSE, pdFALSE, pdMS_TO_TICKS(1000));
        if ((exit_bits & kTaskExitedEvent) == 0) {
            ESP_LOGE(kTag, "Jitter task failed to stop within deadline; aborting to avoid use-after-free");
            std::abort();
        }
        reorder_task_.store(nullptr, std::memory_order_release);
    }
    ClearSlots();
}

bool VoiceUdpMedia::SendAudio(const AudioStreamPacket& packet, uint32_t generation) {
    if (revoked_.load(std::memory_order_acquire)) {
        return false;
    }
    const bool sent = SendPacket(
        voice_udp_wire::kAudio, packet.payload.data(), packet.payload.size(),
        packet.timestamp, generation);
    if (sent && packet.frame_duration == 0 &&
        !revoked_.load(std::memory_order_acquire)) {
        vTaskDelay(pdMS_TO_TICKS(kWakePreRollPacingMs));
    }
    return sent;
}

bool VoiceUdpMedia::IsReady() const {
    return ready_.load(std::memory_order_acquire) &&
           running_.load(std::memory_order_acquire) &&
           !revoked_.load(std::memory_order_acquire);
}

bool VoiceUdpMedia::SetGeneration(uint32_t generation) {
    if (revoked_.load(std::memory_order_acquire)) {
        return false;
    }
    std::lock_guard<std::mutex> lock(slots_mutex_);
    if (revoked_.load(std::memory_order_acquire)) {
        return false;
    }
    const uint32_t current = generation_.load(std::memory_order_acquire);
    if (generation <= current) {
        return generation == current;
    }
    AdvanceGenerationLocked(generation);
    return true;
}

void VoiceUdpMedia::OnAudio(std::function<void(std::unique_ptr<AudioStreamPacket>)> callback) {
    on_audio_ = std::move(callback);
}

bool VoiceUdpMedia::SendPacket(uint8_t flags, const uint8_t* payload, size_t payload_size,
                               uint32_t timestamp, uint32_t generation) {
    if (revoked_.load(std::memory_order_acquire)) {
        return false;
    }
    std::lock_guard<std::mutex> send_lock(send_mutex_);
    if (revoked_.load(std::memory_order_acquire) || udp_ == nullptr ||
        payload_size > kMaxPayloadBytes || send_sequence_ == UINT32_MAX) {
        if (send_sequence_ == UINT32_MAX) {
            ESP_LOGE(kTag, "UDP sequence exhausted; a fresh session is required");
        }
        return false;
    }
    voice_udp_wire::Header header;
    header.flags = flags;
    header.media_id = media_id_;
    header.media_epoch = media_epoch_;
    header.sequence = send_sequence_;
    header.timestamp = timestamp;
    header.generation = generation;
    header.payload_length = static_cast<uint32_t>(payload_size);
    if (!voice_udp_wire::HeaderFieldsValid(
            header, voice_udp_wire::Direction::kUplink)) {
        return false;
    }
    const auto header_bytes = voice_udp_wire::EncodeHeader(header);
    const auto nonce = voice_udp_wire::MakeNonce(uplink_salt_, send_sequence_);

    send_buffer_.resize(voice_udp_wire::kHeaderBytes + payload_size +
                        voice_udp_wire::kTagBytes);
    std::memcpy(send_buffer_.data(), header_bytes.data(), header_bytes.size());
    uint8_t* ciphertext = reinterpret_cast<uint8_t*>(send_buffer_.data()) +
                          voice_udp_wire::kHeaderBytes;
    uint8_t* tag = ciphertext + payload_size;
    // ESP-IDF's accelerated GCM backend rejects a null input pointer even for
    // a valid zero-length authenticated probe.
    static constexpr uint8_t kEmptyPayload = 0;
    const uint8_t* input = payload_size == 0 ? &kEmptyPayload : payload;
    const int64_t crypto_started_us = esp_timer_get_time();
    const int result = mbedtls_gcm_crypt_and_tag(
        &uplink_gcm_, MBEDTLS_GCM_ENCRYPT, payload_size,
        nonce.data(), nonce.size(),
        header_bytes.data(), header_bytes.size(), input, ciphertext,
        voice_udp_wire::kTagBytes, tag);
    const uint32_t crypto_us = static_cast<uint32_t>(esp_timer_get_time() - crypto_started_us);
    if (result != 0) {
        return false;
    }
    tx_crypto_samples_++;
    tx_crypto_us_total_ += crypto_us;
    tx_crypto_us_max_ = std::max(tx_crypto_us_max_, crypto_us);
    bool sent = false;
    for (int attempt = 1; attempt <= kSendAttempts; ++attempt) {
        if (revoked_.load(std::memory_order_acquire)) {
            return false;
        }
        // RequestStop deliberately does not wait for send_mutex_. A revocation
        // that races after this check may allow this one lower-layer Send call
        // to finish, but it prevents every retry and subsequent packet.
        const int send_result = udp_->Send(send_buffer_);
        if (revoked_.load(std::memory_order_acquire)) {
            return false;
        }
        if (send_result == static_cast<int>(send_buffer_.size())) {
            sent = true;
            break;
        }
        if (attempt < kSendAttempts) {
            // Wake-word pre-roll is emitted as a short burst. Give lwIP's
            // bounded pbuf pool time to drain only when it applies backpressure.
            vTaskDelay(pdMS_TO_TICKS(kSendRetryWaitMs));
        }
    }
    if (!sent) {
        ESP_LOGW(kTag, "UDP send backpressure exceeded retries=%d free_internal=%u",
                 kSendAttempts, heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
        return false;
    }
    send_sequence_++;
    if ((tx_crypto_samples_ % 100) == 0) {
        ESP_LOGI(kTag, "TX crypto packets=%lu avg_us=%.1f max_us=%lu free_internal=%u min_internal=%u",
                 tx_crypto_samples_, static_cast<double>(tx_crypto_us_total_) / tx_crypto_samples_,
                 tx_crypto_us_max_, heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                 heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL));
    }
    return true;
}

void VoiceUdpMedia::HandleDatagram(const std::string& datagram) {
    if (revoked_.load(std::memory_order_acquire)) {
        return;
    }
    received_++;
    voice_udp_wire::DatagramView view;
    if (!voice_udp_wire::ParseDatagram(
            reinterpret_cast<const uint8_t*>(datagram.data()), datagram.size(),
            voice_udp_wire::Direction::kDownlink, view) ||
        !voice_udp_wire::MatchesSession(view.header, media_id_, media_epoch_)) {
        invalid_++;
        return;
    }
    const uint32_t sequence = view.header.sequence;
    const uint32_t timestamp = view.header.timestamp;
    const uint32_t generation = view.header.generation;
    const uint32_t payload_size = view.header.payload_length;
    {
        std::lock_guard<std::mutex> lock(slots_mutex_);
        if (revoked_.load(std::memory_order_acquire)) {
            return;
        }
        const uint32_t current_generation = generation_.load(std::memory_order_acquire);
        if (generation < current_generation) {
            invalid_++;
            return;
        }
        if (!AcceptSequence(sequence)) {
            replayed_++;
            return;
        }
    }
    const auto nonce = voice_udp_wire::MakeNonce(downlink_salt_, sequence);
    std::array<uint8_t, kMaxPayloadBytes> plaintext{};
    const int64_t crypto_started_us = esp_timer_get_time();
    const int result = mbedtls_gcm_auth_decrypt(
        &downlink_gcm_, payload_size,
        nonce.data(), nonce.size(),
        view.header_bytes, voice_udp_wire::kHeaderBytes,
        view.tag, voice_udp_wire::kTagBytes, view.ciphertext, plaintext.data());
    const uint32_t crypto_us = static_cast<uint32_t>(esp_timer_get_time() - crypto_started_us);
    if (result != 0 || revoked_.load(std::memory_order_acquire)) {
        invalid_++;
        return;
    }
    rx_crypto_samples_++;
    rx_crypto_us_total_ += crypto_us;
    rx_crypto_us_max_ = std::max(rx_crypto_us_max_, crypto_us);
    bool probe_ack = false;
    {
        std::lock_guard<std::mutex> lock(slots_mutex_);
        if (revoked_.load(std::memory_order_acquire)) {
            return;
        }
        const uint32_t current_generation = generation_.load(std::memory_order_acquire);
        if (generation < current_generation) {
            invalid_++;
            return;
        }
        if (!AcceptSequence(sequence)) {
            replayed_++;
            return;
        }
        if (generation > current_generation) {
            AdvanceGenerationLocked(generation);
        }
        CommitSequence(sequence);
        if (view.header.flags == voice_udp_wire::kProbeAck) {
            ResetReorderCursor(sequence + 1);
            probe_ack = true;
        } else {
            if (!reorder_cursor_initialized_) {
                ResetReorderCursor(sequence);
            }
            InsertPacketLocked(view.header.flags, sequence, timestamp, generation,
                               plaintext.data(), payload_size);
        }
    }
    if (revoked_.load(std::memory_order_acquire)) {
        return;
    }
    if (probe_ack) {
        xEventGroupSetBits(events_, kProbeAckEvent);
    } else if (events_ != nullptr) {
        xEventGroupSetBits(events_, kDataReadyEvent);
    }
    if ((rx_crypto_samples_ % 50) == 0) {
        ESP_LOGI(kTag, "RX crypto packets=%lu avg_us=%.1f max_us=%lu invalid=%lu replay=%lu",
                 rx_crypto_samples_, static_cast<double>(rx_crypto_us_total_) / rx_crypto_samples_,
                 rx_crypto_us_max_, invalid_, replayed_);
    }
}

bool VoiceUdpMedia::AcceptSequence(uint32_t sequence) const {
    if (sequence > receive_highest_) {
        return sequence - receive_highest_ <= kMaxForwardSequenceDistance;
    }
    const uint32_t distance = receive_highest_ - sequence;
    return distance < 64 && (receive_bitmap_ & (uint64_t{1} << distance)) == 0;
}

void VoiceUdpMedia::CommitSequence(uint32_t sequence) {
    if (sequence > receive_highest_) {
        const uint32_t shift = sequence - receive_highest_;
        receive_bitmap_ = shift >= 64 ? 1 : (receive_bitmap_ << shift) | 1;
        receive_highest_ = sequence;
    } else {
        receive_bitmap_ |= uint64_t{1} << (receive_highest_ - sequence);
    }
}

void VoiceUdpMedia::AdvanceGenerationLocked(uint32_t generation) {
    generation_.store(generation, std::memory_order_release);
    ClearSlotsLocked();
    reorder_cursor_initialized_ = false;
}

void VoiceUdpMedia::InsertPacketLocked(uint8_t flags, uint32_t sequence,
                                       uint32_t timestamp, uint32_t generation,
                                       const uint8_t* payload, size_t payload_size) {
    PacketSlot* free_slot = nullptr;
    for (auto& slot : slots_) {
        if (slot.used && slot.sequence == sequence) {
            return;
        }
        if (!slot.used && free_slot == nullptr) {
            free_slot = &slot;
        }
    }
    if (free_slot == nullptr) {
        queue_dropped_++;
        return;
    }
    free_slot->used = true;
    free_slot->flags = flags;
    free_slot->sequence = sequence;
    free_slot->timestamp = timestamp;
    free_slot->generation = generation;
    free_slot->payload_size = static_cast<uint16_t>(payload_size);
    free_slot->arrived_us = esp_timer_get_time();
    if (payload_size > 0) {
        std::memcpy(free_slot->payload.data(), payload, payload_size);
    }
}

void VoiceUdpMedia::ResetReorderCursor(uint32_t sequence) {
    expected_audio_sequence_ = sequence;
    reorder_cursor_initialized_ = true;
}

void VoiceUdpMedia::ReorderLoop() {
    while (running_.load(std::memory_order_acquire) &&
           !revoked_.load(std::memory_order_acquire)) {
        PacketSlot packet;
        bool made_progress = false;
        while (PopExpected(packet)) {
            made_progress = true;
            if (revoked_.load(std::memory_order_acquire)) {
                break;
            }
            if (packet.generation != generation_.load(std::memory_order_acquire)) {
                continue;
            }
            if (packet.flags == voice_udp_wire::kKeepalive) {
                continue;
            }
            auto audio = std::make_unique<AudioStreamPacket>();
            audio->sample_rate = 24000;
            audio->frame_duration = 60;
            audio->timestamp = packet.timestamp;
            audio->payload.assign(packet.payload.begin(), packet.payload.begin() + packet.payload_size);
            if (!revoked_.load(std::memory_order_acquire) && on_audio_ != nullptr) {
                on_audio_(std::move(audio));
            }
            played_++;
            reorder_stack_min_words_ = std::min(
                reorder_stack_min_words_, uxTaskGetStackHighWaterMark(nullptr));
            if ((played_ % 50) == 0) {
                ESP_LOGI(kTag,
                         "playout packets=%lu concealed=%lu lost=%lu dropped=%lu stack_hwm_words=%u",
                         played_, concealed_, lost_, queue_dropped_, reorder_stack_min_words_);
            }
        }
        uint32_t concealed_in_pass = 0;
        uint32_t loss_timestamp = 0;
        while (PopExpiredLoss(loss_timestamp)) {
            if (revoked_.load(std::memory_order_acquire)) {
                break;
            }
            auto loss = std::make_unique<AudioStreamPacket>();
            loss->sample_rate = 24000;
            loss->frame_duration = 60;
            loss->timestamp = loss_timestamp;
            if (!revoked_.load(std::memory_order_acquire) && on_audio_ != nullptr) {
                on_audio_(std::move(loss));
            }
            concealed_++;
            made_progress = true;
            if (++concealed_in_pass >= kMaxConcealmentPerPass) {
                taskYIELD();
                break;
            }
        }
        if (concealed_in_pass > 0) {
            vTaskDelay(1);
        }
        if (!made_progress) {
            xEventGroupWaitBits(
                events_, kStopRequestedEvent | kDataReadyEvent,
                pdFALSE, pdFALSE, pdMS_TO_TICKS(10));
            xEventGroupClearBits(events_, kDataReadyEvent);
        }
    }
    xEventGroupSetBits(events_, kTaskExitedEvent);
}

bool VoiceUdpMedia::PopExpected(PacketSlot& output) {
    std::lock_guard<std::mutex> lock(slots_mutex_);
    if (revoked_.load(std::memory_order_acquire) || !reorder_cursor_initialized_) {
        return false;
    }
    for (auto& slot : slots_) {
        if (slot.used && slot.sequence == expected_audio_sequence_) {
            output = slot;
            slot.used = false;
            expected_audio_sequence_++;
            return true;
        }
    }
    return false;
}

bool VoiceUdpMedia::PopExpiredLoss(uint32_t& timestamp) {
    std::lock_guard<std::mutex> lock(slots_mutex_);
    if (revoked_.load(std::memory_order_acquire) || !reorder_cursor_initialized_) {
        return false;
    }
    PacketSlot* first = nullptr;
    for (auto& slot : slots_) {
        if (slot.used && (first == nullptr || slot.sequence < first->sequence)) {
            first = &slot;
        }
    }
    if (first == nullptr || first->sequence <= expected_audio_sequence_ ||
        esp_timer_get_time() - first->arrived_us < kReorderWaitMs * 1000) {
        return false;
    }
    const uint32_t missing_before_first = first->sequence - expected_audio_sequence_;
    timestamp = first->timestamp >= missing_before_first * kTimestampSamplesPerFrame
                    ? first->timestamp - missing_before_first * kTimestampSamplesPerFrame
                    : 0;
    lost_++;
    expected_audio_sequence_++;
    return true;
}

void VoiceUdpMedia::ClearSlots() {
    std::lock_guard<std::mutex> lock(slots_mutex_);
    ClearSlotsLocked();
}

void VoiceUdpMedia::ClearSlotsLocked() {
    for (auto& slot : slots_) {
        slot.used = false;
    }
}

void VoiceUdpMedia::ReorderTask(void* context) {
    auto* self = static_cast<VoiceUdpMedia*>(context);
    self->ReorderLoop();
    vTaskDelete(nullptr);
}

bool VoiceUdpMedia::DecodeHex(const cJSON* value, uint8_t* output, size_t output_size) {
    if (!cJSON_IsString(value) || std::strlen(value->valuestring) != output_size * 2) {
        return false;
    }
    auto nibble = [](char ch) -> int {
        if (ch >= '0' && ch <= '9') return ch - '0';
        if (ch >= 'a' && ch <= 'f') return ch - 'a' + 10;
        if (ch >= 'A' && ch <= 'F') return ch - 'A' + 10;
        return -1;
    };
    for (size_t index = 0; index < output_size; ++index) {
        const int high = nibble(value->valuestring[index * 2]);
        const int low = nibble(value->valuestring[index * 2 + 1]);
        if (high < 0 || low < 0) return false;
        output[index] = static_cast<uint8_t>((high << 4) | low);
    }
    return true;
}

bool VoiceUdpMedia::DecodeBase64(const cJSON* value, uint8_t* output, size_t output_size) {
    if (!cJSON_IsString(value)) {
        return false;
    }
    size_t decoded = 0;
    const int result = mbedtls_base64_decode(
        output, output_size, &decoded,
        reinterpret_cast<const uint8_t*>(value->valuestring), std::strlen(value->valuestring));
    return result == 0 && decoded == output_size;
}

bool VoiceUdpMedia::ParseExactUint32(const cJSON* value, uint32_t& output) {
    if (!cJSON_IsNumber(value) || !std::isfinite(value->valuedouble) ||
        value->valuedouble < 0 || value->valuedouble > UINT32_MAX ||
        std::floor(value->valuedouble) != value->valuedouble) {
        return false;
    }
    output = static_cast<uint32_t>(value->valuedouble);
    return true;
}
