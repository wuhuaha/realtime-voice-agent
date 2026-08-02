#include "transport_udp/udp_session.h"

#include <algorithm>
#include <cstring>
#include <limits>
#include <new>

#ifdef ESP_PLATFORM
#include <esp_heap_caps.h>
#endif

namespace rva::udp {

namespace {

template <typename T>
void SecureErase(T* value) {
    volatile uint8_t* bytes = reinterpret_cast<volatile uint8_t*>(value);
    for (size_t index = 0; index < sizeof(T); ++index) bytes[index] = 0;
}

}  // namespace

UdpSession::UdpSession(AeadPort& uplink_crypto, AeadPort& downlink_crypto)
    : uplink_crypto_(uplink_crypto), downlink_crypto_(downlink_crypto) {}

void* UdpSession::operator new(size_t size) {
#ifdef ESP_PLATFORM
    void* pointer = heap_caps_malloc(size, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
#else
    void* pointer = ::operator new(size, std::nothrow);
#endif
    if (pointer == nullptr) throw std::bad_alloc();
    return pointer;
}

void UdpSession::operator delete(void* pointer) noexcept {
#ifdef ESP_PLATFORM
    heap_caps_free(pointer);
#else
    ::operator delete(pointer);
#endif
}

UdpSession::~UdpSession() { Revoke(); }

bool UdpSession::Configure(const SessionGrant& grant) {
    std::lock_guard<std::mutex> lock(mutex_);
    RevokeLocked();
    if (!grant.server.valid() || grant.media_epoch == 0 ||
        grant.initial_downlink_generation == 0 ||
        !uplink_crypto_.SetKey(grant.uplink_key) ||
        !downlink_crypto_.SetKey(grant.downlink_key)) {
        RevokeLocked();
        return false;
    }
    grant_ = grant;
    send_sequence_ = 0;
    downlink_generation_ = grant.initial_downlink_generation;
    last_authenticated_receive_us_ = 0;
    replay_.Reset();
    jitter_.Reset(downlink_generation_);
    stats_ = {};
    bound_source_ = {};
    SecureErase(&probe_datagram_);
    probe_size_ = 0;
    ready_ = false;
    playback_active_ = false;
    configured_ = true;
    revoked_ = false;
    return true;
}

void UdpSession::Revoke() {
    std::lock_guard<std::mutex> lock(mutex_);
    RevokeLocked();
}

void UdpSession::RevokeLocked() {
    revoked_ = true;
    ready_ = false;
    configured_ = false;
    playback_active_ = false;
    bound_source_ = {};
    jitter_.Reset(downlink_generation_);
    ClearFutureFrames();
    uplink_crypto_.ClearKey();
    downlink_crypto_.ClearKey();
    SecureErase(&plaintext_);
    SecureErase(&grant_);
    SecureErase(&probe_datagram_);
    probe_size_ = 0;
    send_sequence_ = 0;
    last_authenticated_receive_us_ = 0;
}

bool UdpSession::FenceGeneration(uint32_t generation) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (revoked_ || generation < downlink_generation_) return false;
    playback_active_ = false;
    if (generation > downlink_generation_) {
        downlink_generation_ = generation;
        jitter_.Reset(downlink_generation_);
        for (auto& frame : future_frames_) {
            if (frame.used && frame.generation < downlink_generation_) ClearFutureFrame(&frame);
        }
    } else {
        jitter_.Reset(downlink_generation_);
        ClearFutureFrames();
    }
    return true;
}

bool UdpSession::AdvanceGeneration(uint32_t generation) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (revoked_ || generation < downlink_generation_) return false;
    if (generation > downlink_generation_) {
        downlink_generation_ = generation;
        jitter_.Reset(downlink_generation_);
    }
    // playback generation 只能由 response.begin 激活；媒体包只能提前暂存。
    playback_active_ = true;
    ReleaseFutureAudio(downlink_generation_);
    return true;
}

bool UdpSession::BufferFutureAudio(const wire::Header& header, int64_t now_us) {
    FutureFrame* free = nullptr;
    FutureFrame* oldest_matching = nullptr;
    for (auto& frame : future_frames_) {
        if (frame.used && frame.sequence == header.sequence) return true;
        if (!frame.used && free == nullptr) free = &frame;
        if (frame.used && frame.generation == header.generation &&
            (oldest_matching == nullptr || frame.sequence < oldest_matching->sequence)) {
            oldest_matching = &frame;
        }
    }
    if (free == nullptr) {
        stats_.queue_dropped++;
        if (oldest_matching == nullptr || header.sequence <= oldest_matching->sequence) {
            return false;
        }
        ClearFutureFrame(oldest_matching);
        free = oldest_matching;
    }
    free->used = true;
    free->sequence = header.sequence;
    free->timestamp = header.timestamp;
    free->generation = header.generation;
    free->payload_size = static_cast<uint16_t>(header.payload_length);
    free->arrived_us = now_us;
    std::copy_n(plaintext_.begin(), header.payload_length, free->payload.begin());
    return true;
}

void UdpSession::ReleaseFutureAudio(uint32_t generation) {
    std::array<FutureFrame*, kFutureFrameCount> matching{};
    size_t matching_count = 0;
    for (auto& frame : future_frames_) {
        if (!frame.used) continue;
        if (frame.generation < generation) {
            ClearFutureFrame(&frame);
        } else if (frame.generation == generation) {
            matching[matching_count++] = &frame;
        }
    }
    // The array is intentionally tiny. Bounded insertion sort avoids libstdc++
    // pointer-range diagnostics seen under Xtensa -Werror.
    for (size_t index = 1; index < matching_count; ++index) {
        FutureFrame* current = matching[index];
        size_t position = index;
        while (position > 0 && matching[position - 1]->sequence > current->sequence) {
            matching[position] = matching[position - 1];
            --position;
        }
        matching[position] = current;
    }
    for (size_t index = 0; index < matching_count; ++index) {
        FutureFrame* frame = matching[index];
        jitter_.InsertAudio(frame->sequence, frame->timestamp, frame->generation,
                            frame->payload.data(), frame->payload_size,
                            frame->arrived_us, &stats_);
        ClearFutureFrame(frame);
    }
}

void UdpSession::ClearFutureFrame(FutureFrame* frame) {
    if (frame == nullptr) return;
    SecureErase(frame);
}

void UdpSession::ClearFutureFrames() {
    for (auto& frame : future_frames_) ClearFutureFrame(&frame);
}

bool UdpSession::BuildProbe(uint8_t* output, size_t capacity, size_t* size) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (output == nullptr || size == nullptr || capacity < probe_datagram_.size()) return false;
    if (probe_size_ == 0) {
        if (!Build(wire::DatagramType::kProbe, nullptr, 0, 0, 0,
                   probe_datagram_.data(), probe_datagram_.size(), &probe_size_)) {
            return false;
        }
    }
    std::copy_n(probe_datagram_.begin(), probe_size_, output);
    *size = probe_size_;
    return true;
}

bool UdpSession::BuildKeepalive(uint8_t* output, size_t capacity, size_t* size) {
    std::lock_guard<std::mutex> lock(mutex_);
    return Build(wire::DatagramType::kKeepalive, nullptr, 0, 0, 0,
                 output, capacity, size);
}

bool UdpSession::BuildAudio(const uint8_t* opus, size_t opus_size, uint32_t timestamp,
                            uint8_t* output, size_t capacity, size_t* size) {
    std::lock_guard<std::mutex> lock(mutex_);
    return Build(
        wire::DatagramType::kAudio, opus, opus_size, timestamp, 0,
        output, capacity, size);
}

bool UdpSession::Build(wire::DatagramType type, const uint8_t* payload, size_t payload_size,
                       uint32_t timestamp, uint32_t generation, uint8_t* output,
                       size_t capacity, size_t* size) {
    if (!configured_ || revoked_ || output == nullptr || size == nullptr ||
        payload_size > wire::kMaxPayloadBytes ||
        (payload_size > 0 && payload == nullptr) || send_sequence_ == UINT32_MAX) return false;
    const size_t total = wire::kHeaderBytes + payload_size + wire::kTagBytes;
    if (capacity < total) return false;
    wire::Header header{.type = type,
                        .media_id = grant_.media_id,
                        .media_epoch = grant_.media_epoch,
                        .sequence = send_sequence_,
                        .timestamp = timestamp,
                        .generation = generation,
                        .payload_length = static_cast<uint32_t>(payload_size)};
    if (!wire::HeaderFieldsValid(header, wire::Direction::kUplink)) return false;
    const wire::WireHeader aad = wire::EncodeHeader(header);
    std::memcpy(output, aad.data(), aad.size());
    uint8_t* ciphertext = output + wire::kHeaderBytes;
    uint8_t* tag = ciphertext + payload_size;
    if (!uplink_crypto_.Encrypt(
            wire::MakeNonce(grant_.uplink_salt, send_sequence_), aad,
            payload, payload_size, ciphertext, tag)) return false;
    *size = total;
    send_sequence_++;
    return true;
}

AdmissionResult UdpSession::Receive(const Endpoint& source, const uint8_t* datagram,
                                    size_t size, int64_t now_us) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (revoked_ || !configured_) return AdmissionResult::kRevoked;
    stats_.received++;
    const auto view = wire::ParseDatagram(datagram, size, wire::Direction::kDownlink);
    if (!view) { stats_.invalid_framing++; return AdmissionResult::kInvalidFraming; }
    if (!wire::MatchesSession(view->header, grant_.media_id, grant_.media_epoch)) {
        stats_.wrong_session++;
        return AdmissionResult::kWrongSession;
    }
    if (!replay_.CanAccept(view->header.sequence)) {
        stats_.replayed++; return AdmissionResult::kReplay;
    }
    if (!downlink_crypto_.Decrypt(
            wire::MakeNonce(grant_.downlink_salt, view->header.sequence),
            view->header_bytes, view->ciphertext, view->header.payload_length,
            view->tag, plaintext_.data())) {
        SecureErase(&plaintext_);
        stats_.authentication_failed++; return AdmissionResult::kAuthenticationFailed;
    }
    if ((!bound_source_.valid() &&
         (view->header.type != wire::DatagramType::kProbeAck || source != grant_.server)) ||
        (bound_source_.valid() && source != bound_source_)) {
        SecureErase(&plaintext_);
        stats_.wrong_source++;
        return AdmissionResult::kWrongSource;
    }
    if (!bound_source_.valid()) bound_source_ = source;
    const bool audio = view->header.type == wire::DatagramType::kAudio;
    // Every authenticated packet from the bound source consumes its nonce.
    // Semantic rejection must not leave the same sequence reusable.
    replay_.Commit(view->header.sequence);
    if (!audio && view->header.generation != 0) {
        if (ready_) {
            (void)jitter_.InsertControl(
                view->header.sequence, downlink_generation_, now_us, &stats_);
        }
        SecureErase(&plaintext_);
        stats_.stale_generation++;
        return AdmissionResult::kStaleGeneration;
    }
    last_authenticated_receive_us_ = now_us;
    if (audio && view->header.generation < downlink_generation_) {
        SecureErase(&plaintext_);
        stats_.stale_generation++;
        return AdmissionResult::kStaleGeneration;
    }
    if (audio && view->header.generation > downlink_generation_) {
        const bool can_buffer = view->header.type == wire::DatagramType::kAudio && ready_;
        const bool buffered = can_buffer && BufferFutureAudio(view->header, now_us);
        SecureErase(&plaintext_);
        stats_.future_generation++;
        return !can_buffer || buffered ? AdmissionResult::kFutureGeneration
                                       : AdmissionResult::kQueueFull;
    }
    if (view->header.type == wire::DatagramType::kProbeAck) {
        SecureErase(&plaintext_);
        if (!ready_) {
            ready_ = true;
            jitter_.BeginAt(view->header.sequence + 1);
        } else {
            const auto inserted = jitter_.InsertControl(
                view->header.sequence, downlink_generation_, now_us, &stats_);
            if (inserted != JitterInsertResult::kAccepted) {
                return AdmissionResult::kQueueFull;
            }
        }
        return AdmissionResult::kAcceptedProbeAck;
    }
    if (!ready_) {
        SecureErase(&plaintext_);
        return AdmissionResult::kNotReady;
    }
    if (view->header.type == wire::DatagramType::kAudio && !playback_active_) {
        const bool buffered = BufferFutureAudio(view->header, now_us);
        SecureErase(&plaintext_);
        stats_.future_generation++;
        return buffered ? AdmissionResult::kFutureGeneration : AdmissionResult::kQueueFull;
    }
    if (view->header.type == wire::DatagramType::kKeepalive) {
        SecureErase(&plaintext_);
        const auto inserted = jitter_.InsertControl(
            view->header.sequence, downlink_generation_, now_us, &stats_);
        if (inserted != JitterInsertResult::kAccepted) {
            return AdmissionResult::kQueueFull;
        }
        return AdmissionResult::kAcceptedKeepalive;
    }
    const auto inserted = jitter_.InsertAudio(
        view->header.sequence, view->header.timestamp, view->header.generation,
        plaintext_.data(), view->header.payload_length, now_us, &stats_);
    SecureErase(&plaintext_);
    if (inserted != JitterInsertResult::kAccepted)
        return AdmissionResult::kQueueFull;
    return AdmissionResult::kAcceptedAudio;
}

PlayoutFrame UdpSession::PopPlayout(int64_t now_us) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (revoked_ || !ready_) return {};
    return jitter_.Pop(now_us, &stats_);
}

bool UdpSession::ready() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return ready_ && !revoked_;
}

uint32_t UdpSession::downlink_generation() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return downlink_generation_;
}

Stats UdpSession::stats() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return stats_;
}

int64_t UdpSession::last_authenticated_receive_us() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return last_authenticated_receive_us_;
}

Endpoint UdpSession::server() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return grant_.server;
}

}  // namespace rva::udp
