#pragma once

#include <cstddef>
#include <cstdint>

#include "transport_udp/types.h"

namespace rva::udp {

class AeadPort {
public:
    virtual ~AeadPort() = default;
    virtual bool SetKey(const Aes128Key& key) = 0;
    virtual void ClearKey() = 0;
    virtual bool Encrypt(const wire::Nonce& nonce, const wire::WireHeader& aad,
                         const uint8_t* plaintext, size_t plaintext_size,
                         uint8_t* ciphertext, uint8_t* tag) = 0;
    virtual bool Decrypt(const wire::Nonce& nonce, const uint8_t* aad,
                         const uint8_t* ciphertext, size_t ciphertext_size,
                         const uint8_t* tag, uint8_t* plaintext) = 0;
};

}  // namespace rva::udp
