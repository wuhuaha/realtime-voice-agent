# Release readiness

更新日期：2026-08-01
状态：not ready

本文只记录当前 Product source 的发布门禁。历史 artifact、串口、SSID、临时地址和实验日志不构成当前
release evidence；详细实验材料不进入 Product 仓。

## 最终软件测试 candidate 与 firmware baseline

最终软件测试 candidate 为 commit `986e775`，绑定 root、Server、Desktop、Native/WSS/headless、UDP runner 与 CI gate wiring。
Firmware binary 仍只绑定 build baseline `5d44835`；后续提交未改变 firmware runtime/build inputs，但未在 `986e775` 重建或烧录，因此不得提升 firmware artifact 或 HIL 状态。

| 范围 | 当前状态 | 证据与边界 |
| --- | --- | --- |
| Product repository | `host_verified` | 最终软件测试 candidate `986e775` 的 root suite 为 `37 passed` |
| Server | `host_verified`，外部集成仍有 skip | candidate `986e775` 的完整 suite 为 `314 passed, 3 skipped`；3 项 skipped 不计为通过 |
| Desktop reference client | `host_verified`；distribution `not ready` | candidate `986e775` 本轮完整命令为 `120 passed`；不替代真实声卡、目标 OS distribution 或 native library provenance 验证 |
| Native/WSS host contracts | `host_verified` | candidate `986e775` 的 Native/WSS host tests 与 headless tests 通过；只证明 host 上的 contract、session 和数据路径，不替代 ESP32 真机或真实网络闭环 |
| Native UDP host contracts | `host_verified` / remote CI `not_run` | candidate `986e775` 的本地完整 runner 通过：显式 `IDF_PATH` 先由 source verifier 固定到 ESP-IDF `v5.5.2@30aaf64524299d3bde422ca9a2848090d1bc5d0f`，两组 C++20 host tests 与 GCM 全部 positive/negative vectors 通过；CI gate 已接入且 YAML/pin 语义已验证，remote GitHub CI 为 `pending/not_run`；该结果不替代 UDP 真机/HIL |
| Native ESP-IDF composition | `build_passed` / `image_sized` | commit `5d44835` 使用 ESP-IDF 5.5.2 构建通过；application binary SHA-256 为 `FBCC6E308B77219CFB7FC022903169894E9C9A49B87167C487334E60C19C4369`，size `0x21a180`，4 MiB app 分区余量 `0x1e5e80`（47%）；DIRAM 使用 `202751 / 341760` bytes，IRAM 使用 `16384 / 16384` bytes |

上述结果区分最终软件测试 candidate 与 firmware build baseline。IRAM 已使用 `16384 / 16384` bytes，应作为容量边界持续关注；
build 和 host test 不能推导设备、声学、弱网、端到端延迟或稳定性通过。

## 当前发布门禁

| Gate | 当前状态 | 完成条件 |
| --- | --- | --- |
| Native clean build + size | `passed` | Firmware binary 只绑定 build baseline `5d44835` 的 ESP-IDF 5.5.2 binary identity、size 和 memory usage；后续至 `986e775` 的提交未改变 firmware runtime/build inputs，但未重建该 binary |
| Current image flash/boot | `not_run` | 将上述 SHA-256 对应 image 烧录到目标板，并保存可关联到 commit 与 binary identity 的启动证据 |
| Boot/display/touch | `not_run` | 当前 image 完成 boot、display、touch 和完整交互矩阵；历史板端观察不得外推 |
| Wi-Fi/NVS/bootstrap | `not_run` | 当前 image 完成首次联网、NVS 保存与重启回读、bootstrap、重连和 Wi-Fi flap 验证 |
| Provider chain on current image | `not_run` | 当前 image 完成可追溯的 ASR -> LLM -> TTS 真机闭环；host/headless 结果不替代该 gate |
| WSS voice loop | `not_run` | 当前 image 完成多轮上行、ASR final、完整 TTS、playback、explicit cancel 与重连闭环 |
| UDP voice loop | host `passed` / remote CI `not_run` / device `incomplete` | candidate `986e775` 的本地 UDP runner 已通过，CI gate 已接入且 YAML/pin 语义已验证，remote GitHub CI 为 `pending/not_run`；UDP 真机/HIL 仍为 `not_run`，须由同源重建并烧录的 image 完成 fresh bootstrap、媒体双向传输、模式切换与异常恢复 |
| End-to-end latency | `not_run` | 对当前 image 按既定样本量采集端云 trace，报告口径、分位数和原始证据；历史交互样本不得作为当前 SLA |
| AEC/acoustic | `not_run` | 当前 image 的近讲、远讲、double-talk、自回授与 interrupt tail 评测通过 |
| Stability | `incomplete` | 当前 image 尚未执行 HIL 长稳；完成既定多轮与持续运行门禁，并核对 queue、deadline、disconnect、WDT 和资源水位 |
| Desktop distribution | `not ready` | 按目标 OS/architecture/artifact 完成 PyAV、FFmpeg、libopus、sounddevice、PortAudio 及传递 native libraries 的许可证、notice、SBOM 与 provenance 核查 |
| Security/repository | `not ready` | 保持 repository verifier、secret scan、测试、文档链接和 `git diff --check` 通过，并关闭发布许可证阻塞项 |

`5d44835` firmware build baseline image 尚未烧录到目标板；后续提交虽未改变 firmware runtime/build inputs，但 `986e775` 未重建 firmware binary。
当前 UDP 真机/HIL、端到端 latency 和长稳均没有完成，设备相关 gate 只能由最终 source 与同源 binary identity 对应的新证据关闭。

## 历史 immutable snapshots（不计入当前门禁）

| Snapshot | 历史证据 | 当前适用边界 |
| --- | --- | --- |
| Product/Firmware `8583ef3947e85ca2bf3260de3f5dcfea99ab4360` | 历史 immutable snapshot 曾完成 host suites、ESP-IDF build/size 及 WSS/UDP 板端观察；其 application SHA-256、size、串口和端云 trace 均绑定该旧 source/artifact | 不是当前 runtime/build baseline，不得称为“当前 firmware”或“当前 artifact”，也不得据此提升 `5d44835` 的 flash、HIL、latency、acoustic 或 stability gate |
| Server `314d673` | 历史 immutable server release snapshot，记录过 Opus bad-packet admission、session deadline ownership 和 WSS media sequence 修复及当时的部署/readiness 观察 | 不是当前 Server release；历史部署、readiness 与 provider 观察不能替代当前 source、`5d44835` runtime/build baseline、环境或设备验证 |

`314d673` 记录的三项实现修复已进入后续代码谱系，但“修复存在于当前 source”只属于 source inspection/测试
结论，不会把旧 release 的部署状态或旧 HIL 自动继承给当前 source line：

- authenticated 但无法解码的单个 Opus 包只丢包；连续 8 个坏包才以
  `1011/media_decode_failed` 关闭，decoder 的其他 `RuntimeError` 仍按 runtime failure 处理。
- VoiceSession 与 runner 不再嵌套竞争相同 deadline；父任务取消时会回收 child，避免遗留 task 和重连期间短暂
  `session_overloaded`。
- WSS 下行 media sequence 按方向、按 session 严格递增；Firmware 不再在每个 `response.begin` 重置 sequence。

旧 `8583ef3` firmware 的 UDP 长会话、WSS fallback、wake word、provider chain、设备交互延迟和短时稳定性记录
均只属于该 immutable snapshot。它们可用于设计诊断与回归范围，不能外推为 `5d44835` 的 HIL、端到端 latency、
声学或长稳结果。

## Clean-slate v2 边界

Current runtime 不保留 RVA v1/Xiaozhi dual stack。Canonical `udp-opus-gcm-v2` byte fixtures 位于
`protocol/udp_opus_gcm_v2/`；旧 route、profile 或 wire version 必须 fail closed。已退役实现只由 Git 历史和 ADR
记录，不参与构建、部署或发布验收。

## v1 artifact 的历史 HIL 结论

以下事实用于解释硬件和资源基线，不构成当前 v2 artifact 的 release evidence：

- `esp_audio_codec` 的 Opus encode 在历史配置下首帧累计使用约 26 KiB task stack。历史 24 KiB 配置发生
  `InstrFetchProhibited`；该 v1 artifact 的 legacy 单体 uplink task 当时配置为 36 KiB，按该次 HIL 数据保留约
  10 KiB 余量。当前拆分后的 framer、encoder、sender task 不能继承该整体余量结论，仍须在当前 artifact 的完整
  ASR/TTS 交互和长稳测试中分别采样 high-water mark。
- Worker 的 10 秒 WebSocket PING 会作为零长度 `WEBSOCKET_EVENT_DATA` 到达 ESP-IDF callback，同时由组件自动
  回复 PONG。Transport adapter 必须忽略 PING/PONG/CLOSE data callback，并只通过 CLOSED/DISCONNECTED 事件驱动
  session teardown；否则会稳定形成约 10 秒断连。
- 历史诊断用 guard、逐帧 watermark 和首次 encode 日志已从后续镜像删除；容量修复、stack baseline、Opus frame
  contract 和 control-frame admission 保留。原始串口与服务日志属于 ignored HIL artifact，不进入发布物。
