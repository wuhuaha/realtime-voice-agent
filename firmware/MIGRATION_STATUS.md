# Firmware migration status

状态：`production_source_consolidated | clean_build_verified | current_image_boot_verified | release_evidence_incomplete`
更新日期：2026-07-20

## Production composition

`firmware/targets/lichuang-dev/` 是唯一 production firmware composition。它由以下受控部分组成：

- `xiaozhi-esp32@7b190b78e4f8dfef14126f6cd478c134b3cd3cd8` pinned upstream；
- 仓内 `overlay/`、`overlay-managed/` 与 `overlay-files/`；
- `firmware/locks/xiaozhi-esp32.dependencies.lock`；
- materialize、source contract、local config 与 build 脚本。

上游源码仍 materialize 到 ignored `external/xiaozhi-esp32/`，不成为 tracked production source。原目录
`firmware/reference/xiaozhi-overlay/` 只作为 `migration/baseline/source-manifest.yaml` 中的历史
`source_path` 保留，不再是运行、构建或 CI 入口。

`firmware/device/` 是 non-release component-extraction prototype。其 `voice_contracts`、`voice_core`、host tests
和 headless build 用于验证未来 owner 边界，当前不参与 release image composition，也不是 production target 的
替代品。

## 已收口

- pinned upstream、完整 overlay/managed overlay、UDP production files、构建与 contract 工具已整体提升到
  production target；
- 根 `protocol/xiaozhi_udp_v1/fixtures` 仍是 wire fixture 的单一 authoring source；
- root bootstrap、verify、pytest、CI source contract 与本地操作文档均指向唯一 production target；
- provenance 用 `source_path` 保留历史位置，用 `production_path` 指向当前可校验文件。

源码目录收口只解决 production source ownership，不代表 production ready。下述当前镜像证据与旧 artifact 分开记录。

## 当前 production image 证据

- 当前 `0011..0014` 与 managed `0003..0008` composition 已从 fresh pinned source 顺序应用，且与 applied
  checkout byte-equivalent；ESP-IDF 5.5.2 全量 clean build 完成 `2215/2215`，`xiaozhi.bin` 为
  `2,970,272` bytes，SHA-256 为
  `61542dad78a11a130263952e4148f9b7c70b1e8919e3f2ca192d21612e6716a3`，app partition 余量约 28%，
  DIRAM 为 `170,991 / 341,760` bytes（50.03%）。
- 同一 partition layout 的历史 predecessor 已在 COM11 完成 bootloader、partition table、otadata、app 与 assets
  五区域写入并 hash verified；这些历史区域 hash 不冒充当前 app identity。
- 当前 `61542...` clean-build app 已单独写入 COM11 的 `0x20000`，esptool 报告 hash verified。电脑 TTS 唤醒词
  被板端检测，设备完成公网 Director bootstrap、WSS session、AFE AEC、ASR“请用一句话介绍你自己”、流式字幕和
  `listening -> speaking -> listening`；板端 playback 100 帧、underrun 0、max write 62.3 ms，无 ERROR/panic/WDT。
  该 smoke 达到限定范围的 `boot_observed | device_verified | public_path_verified`，不是正式声学或延迟 SLO。
- superseded `9026...` 中间 artifact 曾完成公网 Director/WSS、AFE AEC、真机 ASR 与流式字幕且无 panic/WDT；
  Server 观察到 FunASR、DeepSeek、remote CosyVoice 与 `reason=user_initiated` close，但关闭来源无法确定为物理
  触屏。这些历史 HIL 仅适用于 `9026...`。
- `0014-ui-control-responsiveness.patch` 将 `assistant_role` 文案改为“AI”，并将 `MAIN_EVENT_SEND_AUDIO` 每轮处理
  限制为最多 4 包；toggle 先执行，停止时同轮跳过发送、清空待发/时间戳/编码上行，并以 generation fence
  拒绝在途旧编码包。build 前的 `test-ui-control-contract.ps1` 已验证具体函数块、事件顺序、有界批处理与禁止
  unbounded `while`。这些是
  `source_contract/build_passed` 证据，不证明物理屏“AI”视觉或聆听按钮点击结束。

## Transport 与 provider 诊断证据

- WSS 诊断镜像通过一次性启动触发完成 Director bootstrap、grant consume、WSS handshake、设备麦克风上行、
  FunASR final、DeepSeek streaming、远端 CosyVoice first PCM、字幕与板端播放；50 帧观察窗口 underrun 为 0。
- UDP 诊断镜像以 `force_udp_for_test` 启动，完成 Director bootstrap、WSS control、首次 authenticated UDP probe、
  8092 media ready、AES-GCM Opus 上行、FunASR/DeepSeek/CosyVoice、UDP 下行与板端播放。端侧加密 600 包窗口
  平均约 434 us、峰值约 1.314 ms，内部 SRAM 最低约 24.7 KiB。
- UDP 代表轮次中 speech end 到 ASR final 约 500 ms、LLM TTFT 约 520 ms、TTS first PCM 到 agent audio
  约 297 ms；这些是单轮诊断值，不是 p95/p99 或正式 SLO。
- 两个诊断镜像与 superseded `9026...` artifact 证明各自当时的端云链路；这些历史结果
  不能替代当前 artifact 的 UDP HIL、UI/触摸或正式声学验收。

## 既有证据

- 旧 `0001..0010` composition 的独立 clean build：`build_passed | image_sized`，`2215/2215`，app
  `0x2d2660` bytes，最小 app partition 余量 28%，DIRAM `170,887 / 341,760` bytes（50.0%）；
- 对应 artifact 已在 COM11 的 ESP32-S3 revision 0.2 上完成 flash read-back、cold boot、Wi-Fi、display/audio
  初始化、唤醒、WSS handshake、UDP GCM probe 与 600+ UDP Opus uplink：`device_verified`；
- `firmware/device` contracts/core host tests：`host_verified`，headless image：`build_passed | image_sized`。

这些证据对应 `migration/baseline/` 记录的历史 composition/artifact，不能自动升级为当前组合的证据。
当前 `0011..0014` 与 managed `0003..0008` 的独立 build/flash/boot 结果已在上一节另行记录。

## 当前未运行

- 最终 clean-build artifact 的 UDP provider 闭环：`not_run`；公网 host authenticated UDP probe 与历史诊断
  镜像不能替代当前 artifact 的 UDP HIL；
- 当前 `61542...` artifact 的人工近讲、连续多轮与点击结束 HIL：`not_run`；
- UI 视觉与触摸验收：`not_run`；
- 物理屏 `assistant_role` 是否显示“AI”、聆听按钮点击能否结束会话：`not_run`；
- near/far/double-talk、20 轮、弱网与 30 分钟长稳：`not_run`。

只有这些门禁分别产生匹配 artifact 的证据后，才能提升相应验证等级；CI 的
`production-source-contract` job 只验证 source/config/contracts，不冒充 clean build。
