#include "transport_udp/gcm_crypto.h"

#include <mbedtls/cipher.h>

namespace rva::udp {

MbedTlsGcm::MbedTlsGcm() { mbedtls_gcm_init(&context_); }
MbedTlsGcm::~MbedTlsGcm() {
    ClearKey();
    mbedtls_gcm_free(&context_);
}

bool MbedTlsGcm::SetKey(const Aes128Key& key) {
    ClearKey();
    keyed_ = mbedtls_gcm_setkey(
        &context_, MBEDTLS_CIPHER_ID_AES, key.data(), key.size() * 8) == 0;
    return keyed_;
}

void MbedTlsGcm::ClearKey() {
    mbedtls_gcm_free(&context_);
    mbedtls_gcm_init(&context_);
    keyed_ = false;
}

bool MbedTlsGcm::Encrypt(const wire::Nonce& nonce, const wire::WireHeader& aad,
                         const uint8_t* plaintext, size_t plaintext_size,
                         uint8_t* ciphertext, uint8_t* tag) {
    static constexpr uint8_t kEmpty = 0;
    if (!keyed_ || ciphertext == nullptr || tag == nullptr) return false;
    const uint8_t* input = plaintext_size == 0 ? &kEmpty : plaintext;
    return input != nullptr && mbedtls_gcm_crypt_and_tag(
        &context_, MBEDTLS_GCM_ENCRYPT, plaintext_size, nonce.data(), nonce.size(),
        aad.data(), aad.size(), input, ciphertext, wire::kTagBytes, tag) == 0;
}

bool MbedTlsGcm::Decrypt(const wire::Nonce& nonce, const uint8_t* aad,
                         const uint8_t* ciphertext, size_t ciphertext_size,
                         const uint8_t* tag, uint8_t* plaintext) {
    static constexpr uint8_t kEmpty = 0;
    if (!keyed_ || aad == nullptr || ciphertext == nullptr || tag == nullptr ||
        plaintext == nullptr) return false;
    const uint8_t* input = ciphertext_size == 0 ? &kEmpty : ciphertext;
    return mbedtls_gcm_auth_decrypt(
        &context_, ciphertext_size, nonce.data(), nonce.size(), aad,
        wire::kHeaderBytes, tag, wire::kTagBytes, input, plaintext) == 0;
}

}  // namespace rva::udp
