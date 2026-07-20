#include "voice_contracts/udp_wire.h"

#include <algorithm>
#include <array>
#include <charconv>
#include <cstdint>
#include <string_view>
#include <vector>

namespace {

namespace wire = voice::contracts::udp_v1;

int HexNibble(char value) {
    if (value >= '0' && value <= '9') return value - '0';
    if (value >= 'a' && value <= 'f') return value - 'a' + 10;
    if (value >= 'A' && value <= 'F') return value - 'A' + 10;
    return -1;
}

bool ParseHex(std::string_view text, std::vector<std::uint8_t>& output) {
    if ((text.size() % 2) != 0) return false;
    output.resize(text.size() / 2);
    for (std::size_t index = 0; index < output.size(); ++index) {
        const int high = HexNibble(text[index * 2]);
        const int low = HexNibble(text[index * 2 + 1]);
        if (high < 0 || low < 0) return false;
        output[index] = static_cast<std::uint8_t>((high << 4) | low);
    }
    return true;
}

bool ParseU32(std::string_view text, std::uint32_t& output) {
    const auto result =
        std::from_chars(text.data(), text.data() + text.size(), output);
    return result.ec == std::errc{} &&
           result.ptr == text.data() + text.size();
}

bool ParseDirection(std::string_view text, wire::Direction& output) {
    if (text == "uplink") {
        output = wire::Direction::kUplink;
        return true;
    }
    if (text == "downlink") {
        output = wire::Direction::kDownlink;
        return true;
    }
    return false;
}

int Positive(int argc, char** argv) {
    if (argc != 15) return 2;
    std::vector<std::uint8_t> datagram;
    std::vector<std::uint8_t> expected_header;
    std::vector<std::uint8_t> expected_ciphertext_and_tag;
    std::vector<std::uint8_t> salt_bytes;
    std::vector<std::uint8_t> expected_nonce;
    std::vector<std::uint8_t> media_id_bytes;
    wire::Direction direction = wire::Direction::kUplink;
    if (!ParseHex(argv[2], datagram) || !ParseHex(argv[3], expected_header) ||
        !ParseHex(argv[4], expected_ciphertext_and_tag) ||
        !ParseHex(argv[5], salt_bytes) || !ParseHex(argv[6], expected_nonce) ||
        !ParseDirection(argv[7], direction) ||
        !ParseHex(argv[9], media_id_bytes) ||
        expected_header.size() != wire::kHeaderBytes ||
        salt_bytes.size() != wire::kSaltBytes ||
        expected_nonce.size() != wire::kNonceBytes ||
        media_id_bytes.size() != wire::kMediaIdBytes) {
        return 3;
    }

    const auto view = wire::ParseDatagram(datagram.data(), datagram.size(), direction);
    if (!view) return 4;

    std::array<std::uint32_t, 6> expected{};
    constexpr std::array<int, 6> kNumericArguments = {8, 10, 11, 12, 13, 14};
    for (std::size_t index = 0; index < expected.size(); ++index) {
        if (!ParseU32(argv[kNumericArguments[index]], expected[index])) return 5;
    }
    wire::MediaId expected_media_id{};
    std::copy(media_id_bytes.begin(), media_id_bytes.end(),
              expected_media_id.begin());
    if (static_cast<std::uint8_t>(view->header.type) != expected[0] ||
        view->header.media_epoch != expected[1] ||
        view->header.sequence != expected[2] ||
        view->header.timestamp != expected[3] ||
        view->header.generation != expected[4] ||
        view->header.payload_length != expected[5] ||
        !wire::MatchesSession(view->header, expected_media_id, expected[1])) {
        return 6;
    }

    const auto encoded_header = wire::EncodeHeader(view->header);
    if (!std::equal(encoded_header.begin(), encoded_header.end(),
                    expected_header.begin()) ||
        !std::equal(expected_header.begin(), expected_header.end(),
                    datagram.begin())) {
        return 7;
    }

    wire::DirectionalSalt salt{};
    std::copy(salt_bytes.begin(), salt_bytes.end(), salt.begin());
    const auto nonce = wire::MakeNonce(salt, view->header.sequence);
    if (!std::equal(nonce.begin(), nonce.end(), expected_nonce.begin())) return 8;

    const std::size_t protected_size =
        view->header.payload_length + wire::kTagBytes;
    if (expected_ciphertext_and_tag.size() != protected_size ||
        view->header_bytes != datagram.data() ||
        view->ciphertext != datagram.data() + wire::kHeaderBytes ||
        view->tag != view->ciphertext + view->header.payload_length ||
        !std::equal(view->ciphertext, view->ciphertext + protected_size,
                    expected_ciphertext_and_tag.begin())) {
        return 9;
    }
    return 0;
}

int Admission(int argc, char** argv, bool expected) {
    if (argc != 3) return 2;
    std::vector<std::uint8_t> datagram;
    const bool admitted = ParseHex(argv[2], datagram) &&
        wire::ParseDatagram(
            datagram.data(), datagram.size(), wire::Direction::kUplink)
            .has_value();
    return admitted == expected ? 0 : 10;
}

int TypedBoundaries(int argc) {
    if (argc != 2) return 2;
    wire::Header keepalive;
    keepalive.type = wire::DatagramType::kKeepalive;
    keepalive.media_epoch = 1;
    keepalive.sequence = 7;
    keepalive.timestamp = 123456;
    keepalive.generation = 1;
    const auto bytes = wire::EncodeHeader(keepalive);
    std::array<std::uint8_t, wire::kHeaderBytes + wire::kTagBytes> datagram{};
    std::copy(bytes.begin(), bytes.end(), datagram.begin());
    if (!wire::ParseDatagram(
            datagram.data(), datagram.size(), wire::Direction::kUplink) ||
        !wire::ParseDatagram(
            datagram.data(), datagram.size(), wire::Direction::kDownlink)) {
        return 11;
    }

    keepalive.media_epoch = 0;
    if (wire::HeaderFieldsValid(keepalive, wire::Direction::kUplink)) return 12;
    keepalive.media_epoch = 1;
    keepalive.generation = 0;
    if (!wire::HeaderFieldsValid(keepalive, wire::Direction::kUplink)) return 13;
    keepalive.generation = 1;
    keepalive.type = static_cast<wire::DatagramType>(0x03);
    if (wire::HeaderFieldsValid(keepalive, wire::Direction::kUplink)) return 14;
    keepalive.type = wire::DatagramType::kProbeAck;
    if (wire::HeaderFieldsValid(keepalive, wire::Direction::kUplink)) return 15;
    return 0;
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 2) return 2;
    const std::string_view mode(argv[1]);
    if (mode == "positive") return Positive(argc, argv);
    if (mode == "parser-reject") return Admission(argc, argv, false);
    if (mode == "auth-candidate") return Admission(argc, argv, true);
    if (mode == "typed-boundaries") return TypedBoundaries(argc);
    return 2;
}
