# Firmware migration status

状态：`reference_migrated | target_contract_skeleton_only`

## 已迁移

- 固定 `xiaozhi-esp32@7b190b78e4f8dfef14126f6cd478c134b3cd3cd8` 的 reference overlay、
  managed patches、UDP production files、构建与 contract 工具；
- 受控完整 `dependencies.lock` 和从 `third_party/sources.lock.yaml` materialize/verify 的入口；
- 根 `protocol/xiaozhi_udp_v1/fixtures` 的单一消费路径；
- 可 host-test、可 ESP-IDF headless build 的 `voice_contracts` 与 `voice_core` 边界。

## 尚未抽取

以下实现仍由 reference Xiaozhi monolith 拥有，未迁移到目标 component，也不得描述成已完成：

- 立创板 I2C/I2S、ES8311/ES7210、PCA9557、LCD、touch bring-up；
- AudioService、AFE/device AEC、WakeNet、Opus encoder/decoder task 与 queue；
- WSS control/media、UDP GCM socket/jitter/concealment 的 concrete transport；
- `VoiceAgentDisplay`、LVGL 主界面、Wi-Fi/endpoint provisioning；
- target component 与 reference runtime 的 production composition。

在上述 implementation 逐批抽取并完成 reference parity、clean build、size 和受影响 HIL 前，
`firmware/device` 不能用于发布或烧录，reference overlay 仍是唯一功能基线。

## 证据边界

本迁移不继承旧固件 HIL。最终 reference `0001..0010` 在研究仓只有 clean build/size 证据；
新仓 materialize、reference clean build、flash、boot、WSS/UDP、声学、弱网和长稳必须分别记录。

## 2026-07-20 迁移验证

- `materialize-upstream.py` 的隔离 local Git clone、固定 revision、完整 lock 和篡改拒绝：`host_verified`；
- 新仓实际 `third_party/sources.lock.yaml` 与受控 dependency lock SHA256：`contract_verified`；
- 固定 revision 已准备源码上的 source、endpoint、UDP lifecycle 和 canonical fixture contract：
  `contract_verified`；新仓 `external/xiaozhi-esp32` 尚未 materialize；
- `voice_contracts`、`voice_core` host C++17 边界测试：`host_verified`；
- ESP-IDF 5.5.2、GCC 14.2.0、`esp32s3` minimal headless build：`build_passed | image_sized`；
  app `0x27740` bytes，1 MiB headless partition 余量 `0xd88c0` bytes（85%），DIRAM
  `53,271 / 341,760` bytes（15.59%）。该数值只适用于 contract skeleton，不能与 reference app 比较；
- reference clean build、flash、boot、真实 WSS/UDP、UI、音频、AEC、声学、弱网和长稳：`not_run`。
