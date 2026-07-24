# Release readiness

更新日期：2026-07-24
状态：not ready

本文只记录当前 Product source 的发布门禁。历史迁移 artifact、串口、SSID、临时地址和实验日志不构成当前
release evidence；详细实验材料不进入 Product 仓。

## 当前软件证据

| 范围 | 状态 | 证据 |
| --- | --- | --- |
| Product repository | `verified`（runtime `314d6734a306c2735eeefe2ae69b819f8c70a981`） | Ruff、repository verifier、secret scan、根测试 `46 passed` 和 `git diff --check` 已通过；提交时仍须复核 staged 内容 |
| `rva-control-v2` schema/fixtures | `contract_verified` | canonical schema、semantic fixtures 与 UDP GCM v2 byte vectors 已由当前根测试覆盖；不替代真实网络/设备闭环 |
| Director grant/fencing | `unit_verified` / `contract_verified` | 完整 Server suite 覆盖 grant、replay、route 与 fencing；真实 Redis 集成 3 项因未配置测试地址而 skipped，不计为通过 |
| Worker `/v2/voice`、RVA runtime | `unit_verified` / `contract_verified` | 完整 Server suite 覆盖 v2 control、response identity、playback facts、cancel ledger、provider 和 teardown 边界 |
| Worker RVA UDP binding | `contract_verified` / `public_path_verified`（历史组合） | v2 GCM fixture、gateway probe ACK 和 grant refresh 测试通过；`refresh_after_ms` 是相对延迟，设备以 monotonic clock 调度，不依赖 SNTP；`314d673` 的最终媒体 HIL 尚未执行 |
| Server static analysis/tests | `verified` | Ruff 通过；Opus runtime focused tests `20 passed`，生命周期/runtime/agent focused tests `35 passed`；使用 `server/.venv` 复跑完整 Server suite 为 `234 passed, 3 skipped`，0 failed。Windows 子进程退出确认用例此前曾偶发 5 秒超时，本轮复跑通过，作为稳定性关注项保留；3 个 Redis 环境 skip 仍不计为集成通过 |
| Firmware core/session contracts | `contract_verified` / host tests `not_run` | 根测试覆盖协议级约束，并新增跨两个 response 的 WSS sequence 断言；本机缺 host compiler，C/C++ host contract test 未执行 |
| WSS protocol/owner | `contract_verified` / device `not_run` | 下行 media sequence 已统一为按方向、按 session 严格单调；当前固件尚未烧录，第二条 response 的真机回归未执行 |
| Board/audio/UI components | `build_passed` / current boot `not_run` | focused component/Xtensa composition 已构建；LCD/LVGL、Qwen 字体和 audio codec 仅有上一 artifact 的启动证据，当前 artifact 尚未烧录 |
| Native ESP-IDF composition | `build_passed` / `image_sized` | ESP-IDF 5.5.2 构建通过，application `0x214e60`、4 MiB app 分区余量约 48%，version 为 `314d673-dirty`；该 identity 只用于本次本地构建追踪，不是已烧录发布制品 |
| Native WSS/UDP voice loop | `public_path_verified`（历史组合）/ current HIL `not_run` | 历史组合曾完成真实 ASR→LLM→MiMo→完整 playout；`314d673` 固件尚未烧录，最终 WSS/UDP 闭环和至少 12 分钟长稳均未执行 |

以上跨进程闭环只证明当前主机上的 Server 拓扑、协议和 provider 数据路径可运行，不替代目标部署、ESP32 真机、
声学、弱网或长稳证据。测试过程中使用的临时地址、进程标识、凭据和原始日志不进入 Product 文档。

当前 `ol` 验证环境已部署
`/home/ubuntu/services/realtime-voice-agent/releases/314d6734a306c2735eeefe2ae69b819f8c70a981`，实际
`EnvironmentFile` 使用 `VOICE_WORKER_ID=worker-ol-314d673`；Director/Worker readiness 和 UDP ready 已通过。
部署 archive 为 `rva-314d673.tar.gz`，SHA256
`2b8483b654d5c6cab6bc727a7547a8436fb8888188786327fecfd70fccb47ca4`。这些事实证明 source identity、进程
incarnation 和基础 readiness，不证明 ESP32 媒体 HIL 或正式 release gate 已通过。

`314d673` 收敛了三项会造成端云反复断连或第二轮失败的实现问题：

- authenticated 但无法解码的单个 Opus 包不再立即升级为 `1002 protocol_error`；单包丢弃，连续 8 个坏包才以
  `1011/media_decode_failed` 关闭，decoder 的其他 `RuntimeError` 仍按 runtime failure 处理。
- VoiceSession 与 runner 不再嵌套竞争相同 deadline；父任务取消时会回收 child，避免遗留 task 和重连期间短暂
  `session_overloaded`。
- WSS 下行 media sequence 统一为按方向、按 session 严格递增；Firmware 不再在每个 `response.begin` 重置 sequence，
  修复第二条 TTS response 首包触发 `wss_media_admission` 的确定性失败。

## 发布门禁

| Gate | 当前状态 | 完成条件 |
| --- | --- | --- |
| Native clean build + size | `build_passed` / `image_sized` | `314d673` 已完成 Xtensa build 与 size；发布前仍需 clean-checkout rebuild 并记录最终 firmware SHA256，证明依赖恢复和制品身份可复现 |
| Boot/display/touch | current artifact `not_run` | 开发板当前不在现场，`314d673` 固件未烧录；启动、LCD/LVGL、中文字体、touch 与 mode UI 均不得沿用旧 artifact 记为通过 |
| Wi-Fi/NVS/bootstrap | current artifact `not_run` | 当前固件未烧录；自动连接、双网络 fallback、公网 bootstrap/grant、保存及重启回读须在同一最终 artifact 上复核 |
| WSS voice loop | current artifact `not_run` | 至少完成两条真实回复，验证 session sequence 跨 response 单调，并覆盖 ASR final、LLM、完整 TTS、explicit cancel 与断线恢复 |
| UDP voice loop | current artifact `not_run` | 完成 GCM probe、双向媒体、generation、到期 fresh bootstrap、loss/jitter/PLC 与模式切换；单坏 Opus 包不得再关闭 session |
| AEC/acoustic | `not_run` | 近讲、远讲、double-talk、自回授与 interrupt tail 评测通过 |
| Stability | `not_run` | 先完成至少 12 分钟，跨越旧约 570 秒故障边界，确认无 `1002 protocol_error`、`wss_media_admission`、unhandled task 或异常 grant refresh；正式发布仍须完成 20 轮和 30 分钟运行，无 panic/WDT、泄漏或持续 underrun |
| Security/repository | `not ready` | repository verifier、secret scan、根测试、protocol 测试和 `git diff --check` 通过；`xiaozhi-fonts` 固定包只声明 MIT metadata、未附上游许可证文本，发布前必须完成许可证来源复核 |

## Clean-slate v2 边界

Current runtime 不保留 RVA v1/Xiaozhi dual stack。Canonical `udp-opus-gcm-v2` byte fixtures 位于
`protocol/udp_opus_gcm_v2/`；旧 route、profile 或 wire version 必须 fail closed。历史 migration provenance
只用于来源追溯，不参与构建、部署或发布验收。

## 2026-07-22 v1 artifact 的历史 HIL 结论

以下事实用于解释硬件和资源基线，不构成当前 v2 artifact 的 release evidence：

- `esp_audio_codec` 的 Opus encode 在本配置下首帧累计使用约 26 KiB task stack。历史 24 KiB 配置发生
  `InstrFetchProhibited`；当前 uplink task 配置为 36 KiB，按该次 HIL 数据保留约 10 KiB 余量。该数值仍需在最终
  artifact 的完整 ASR/TTS 交互和长稳测试中重新采样 high-water mark。
- Worker 的 10 秒 WebSocket PING 会作为零长度 `WEBSOCKET_EVENT_DATA` 到达 ESP-IDF callback，同时由组件自动
  回复 PONG。Transport adapter 必须忽略 PING/PONG/CLOSE data callback，并只通过 CLOSED/DISCONNECTED 事件驱动
  session teardown；否则会稳定形成约 10 秒断连。
- 以上诊断用 guard、逐帧 watermark 和首次 encode 日志已从最终镜像删除；容量修复、stack baseline、Opus frame
  contract 和 control-frame admission 保留。原始串口与服务日志属于 ignored HIL artifact，不进入发布物。
