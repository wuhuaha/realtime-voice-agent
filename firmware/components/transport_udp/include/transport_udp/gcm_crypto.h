#pragma once

#include <mbedtls/gcm.h>

#include "transport_udp/crypto.h"

namespace rva::udp {

class MbedTlsGcm final : public AeadPort {
public:
    MbedTlsGcm();
    ~MbedTlsGcm() override;
    bool SetKey(const Aes128Key& key) override;
    void ClearKey() override;
    bool Encrypt(const wire::Nonce& nonce, const wire::WireHeader& aad,
                 const uint8_t* plaintext, size_t plaintext_size,
                 uint8_t* ciphertext, uint8_t* tag) override;
    bool Decrypt(const wire::Nonce& nonce, const uint8_t* aad,
                 const uint8_t* ciphertext, size_t ciphertext_size,
                 const uint8_t* tag, uint8_t* plaintext) override;
private:
    mbedtls_gcm_context context_{};
    bool keyed_ = false;
};

}  // namespace rva::udp
