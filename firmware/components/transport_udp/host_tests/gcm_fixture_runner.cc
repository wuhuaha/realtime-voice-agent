#include <algorithm>
#include <cassert>
#include <cstdint>
#include <string>
#include <vector>

#include "transport_udp/gcm_crypto.h"

namespace {

using namespace rva::udp;

uint8_t Nibble(char value) {
    if (value >= '0' && value <= '9') return static_cast<uint8_t>(value - '0');
    if (value >= 'a' && value <= 'f') return static_cast<uint8_t>(value - 'a' + 10);
    if (value >= 'A' && value <= 'F') return static_cast<uint8_t>(value - 'A' + 10);
    assert(false);
    return 0;
}

std::vector<uint8_t> Hex(const char* value) {
    const std::string text(value);
    assert(text.size() % 2 == 0);
    std::vector<uint8_t> output(text.size() / 2);
    for (size_t index = 0; index < output.size(); ++index) {
        output[index] = static_cast<uint8_t>(
            (Nibble(text[index * 2]) << 4) | Nibble(text[index * 2 + 1]));
    }
    return output;
}

template <size_t Size>
std::array<uint8_t, Size> Fixed(const char* value) {
    const auto bytes = Hex(value);
    assert(bytes.size() == Size);
    std::array<uint8_t, Size> output{};
    std::copy(bytes.begin(), bytes.end(), output.begin());
    return output;
}

wire::Direction Direction(const char* value) {
    return std::string(value) == "uplink" ? wire::Direction::kUplink
                                           : wire::Direction::kDownlink;
}

}  // namespace

int main(int argc, char** argv) {
    assert(argc >= 2);
    const std::string mode(argv[1]);
    if (mode == "positive") {
        assert(argc == 9);
        const Aes128Key key = Fixed<kAes128KeyBytes>(argv[3]);
        const wire::DirectionalSalt salt = Fixed<wire::kSaltBytes>(argv[4]);
        const auto header = Fixed<wire::kHeaderBytes>(argv[5]);
        const auto payload = Hex(argv[6]);
        const auto expected_ciphertext_tag = Hex(argv[7]);
        const auto expected_nonce = Fixed<wire::kNonceBytes>(argv[8]);
        std::vector<uint8_t> datagram(header.begin(), header.end());
        datagram.insert(datagram.end(), expected_ciphertext_tag.begin(),
                        expected_ciphertext_tag.end());
        const auto view = wire::ParseDatagram(datagram.data(), datagram.size(), Direction(argv[2]));
        assert(view);
        assert(wire::MakeNonce(salt, view->header.sequence) == expected_nonce);

        MbedTlsGcm crypto;
        assert(crypto.SetKey(key));
        std::vector<uint8_t> ciphertext(std::max<size_t>(1, payload.size()));
        std::array<uint8_t, wire::kTagBytes> tag{};
        assert(crypto.Encrypt(expected_nonce, header, payload.data(), payload.size(),
                              ciphertext.data(), tag.data()));
        ciphertext.resize(payload.size());
        ciphertext.insert(ciphertext.end(), tag.begin(), tag.end());
        assert(ciphertext == expected_ciphertext_tag);

        std::vector<uint8_t> plaintext(std::max<size_t>(1, payload.size()));
        assert(crypto.Decrypt(expected_nonce, header.data(), view->ciphertext,
                              view->header.payload_length, view->tag, plaintext.data()));
        plaintext.resize(payload.size());
        assert(plaintext == payload);
        return 0;
    }

    assert(mode == "negative" && argc == 7);
    const Aes128Key key = Fixed<kAes128KeyBytes>(argv[2]);
    const wire::DirectionalSalt salt = Fixed<wire::kSaltBytes>(argv[3]);
    const auto datagram = Hex(argv[4]);
    const std::string stage(argv[5]);
    const auto direction = Direction(argv[6]);
    const auto view = wire::ParseDatagram(datagram.data(), datagram.size(), direction);
    if (stage == "parser") {
        assert(!view);
        return 0;
    }
    assert(stage == "authentication" && view);
    MbedTlsGcm crypto;
    assert(crypto.SetKey(key));
    std::vector<uint8_t> plaintext(std::max<uint32_t>(1, view->header.payload_length));
    assert(!crypto.Decrypt(wire::MakeNonce(salt, view->header.sequence),
                           view->header_bytes, view->ciphertext,
                           view->header.payload_length, view->tag, plaintext.data()));
    return 0;
}
