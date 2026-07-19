# Firmware migration status

状态：`reference_migrated | reference_build_passed | target_contract_skeleton_only`

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

本迁移不继承旧固件 HIL。最终 reference `0001..0010` 的新仓 clean build、flash、boot 和部分
transport HIL 已分别执行并记录；它们不等于目标 component 固件完成，也不替代 UI/触摸、真实声学闭环、
弱网和长稳验证。

## 2026-07-20 迁移验证

- `materialize-upstream.py` 的隔离 local Git clone、固定 revision、完整 lock 和篡改拒绝：`host_verified`；
- 新仓实际 `third_party/sources.lock.yaml` 与受控 dependency lock SHA256：`contract_verified`；
- 新仓 ignored checkout 已 materialize；固定 revision 的 source、endpoint、UDP lifecycle 和 canonical
  fixture contract：`contract_verified`；
- reference `0001..0010` 独立 clean build：`build_passed | image_sized`，`2215/2215`，app
  `0x2d2660` bytes，最小 app partition 余量 28%，DIRAM `170,887 / 341,760` bytes（50.0%）；
  local-config artifact SHA256 为
  `43bac4d4ed678b3298cc9f4c8e9da0c4ab7608af731406cec31939ee457350c8`。该 artifact ignored、
  可能包含 local credentials，hash 不代表 bit-reproducible；
- `voice_contracts`、`voice_core` host C++17 边界测试：`host_verified`；
- ESP-IDF 5.5.2、GCC 14.2.0、`esp32s3` minimal headless build：`build_passed | image_sized`；
  app `0x27740` bytes，1 MiB headless partition 余量 `0xd88c0` bytes（85%），DIRAM
  `53,271 / 341,760` bytes（15.59%）。该数值只适用于 contract skeleton，不能与 reference app 比较；
- reference HIL 使用 `COM11` 上的 ESP32-S3 revision 0.2（8 MiB PSRAM）。先擦除 `0x9000` 起始的
  16 KiB NVS，再烧录 bootloader、partition table、otadata、app 和 assets；五个写入区域的 flash
  read-back SHA256 均与 [artifacts.sha256](../../migration/baseline/artifacts.sha256) 一致。app SHA256 为
  `43bac4d4ed678b3298cc9f4c8e9da0c4ab7608af731406cec31939ee457350c8`：
  `device_verified`；
- cold boot 后连接 Wi-Fi“广告位招租”，获得 `192.168.1.105`；display、audio codec、ES7210、
  AEC、VAD 与 wake model 初始化完成，串口观察窗口内无 panic/WDT：`boot_observed | device_verified`；
- 唤醒成功；WSS 握手约 20 ms；UDP GCM 完成首个 authenticated probe 并进入 ready，随后观察到
  600+ UDP Opus uplink packets；AEC 运行于 `VOIP_HIGH_PERF`：`device_verified`。这些事实证明端侧唤醒和 UDP 上行
  transport，不证明真机 ASR/TTS/downlink playout 闭环；
- UI 视觉/触摸：`not_run`。自动声学语句采集虽已尝试，但采集峰值过低、未触发 ASR，因此真机
  FunASR/DeepSeek/CosyVoice 闭环、near/far/double-talk、20 轮、弱网和 30 分钟长稳仍为 `not_run`；
- Server `259aeee` 的 host synthetic real-media E2E 已收到 FunASR final（约 480 ms）、DeepSeek
  HTTP 200/TTFT（约 9,876 ms）、CosyVoice HTTP 200/TTFB（约 594 ms）和下行音频：
  `host_verified`。该结果使用合成输入，不是上述缺失的真机声学闭环。
