# Release readiness

更新日期：2026-07-24
状态：not ready

本文只记录当前 Product source 的发布门禁。历史迁移 artifact、串口、SSID、临时地址和实验日志不构成当前
release evidence；详细实验材料不进入 Product 仓。

## 当前软件证据

| 范围 | 状态 | 证据 |
| --- | --- | --- |
| Product repository | `verified`（当前工作树） | repository verifier、根测试 46 项和 `git diff --check` 已通过；提交后仍须记录最终 Product commit 并重跑 secret scan |
| `rva-control-v2` schema/fixtures | `contract_verified` | canonical schema、semantic fixtures 与 UDP GCM v2 byte vectors 已由当前根测试覆盖；不替代真实网络/设备闭环 |
| Director grant/fencing | `not_run` | v2-only profile/route 变更后重跑完整 Server suite；Redis 环境 skip 不计为通过 |
| Worker `/v2/voice`、RVA runtime | `not_run` | 需覆盖播放期间 audio→STT、strict explicit policy、单 response identity、atomic fence 与 playback facts |
| Worker RVA UDP binding | `not_run` | 需覆盖 v2 grant/probe/GCM、uplink generation 0、双向媒体和 stop fence |
| Server static analysis | `verified` | Ruff 已通过；完整 Server suite 仍是独立门禁 |
| Firmware core/session contracts | `not_run` | 等待 v2 parser、playback owner 与 canonical UDP fixture host tests |
| WSS protocol/owner | `not_run` | 等待 v3 strict parser、fragment、queue、teardown host tests |
| Board/audio/UI components | `not_run` | v2 变更后需 focused host/Xtensa component compile；不等于 HIL |
| Native ESP-IDF composition | `build_passed` / `image_sized` | ESP-IDF 5.5.2 构建与 size 通过，application 约 `0x214c30`、4 MiB app 分区余量约 48%；发布前仍需 clean checkout 重跑并记录 digest |
| Native WSS/UDP voice loop | `not_run` | 当前 v2 artifact 已烧录且观察到自动 Wi-Fi 连接；稳定公网 bootstrap、WSS/UDP 与 provider 闭环尚未执行 |

以上跨进程闭环只证明当前主机上的 Server 拓扑、协议和 provider 数据路径可运行，不替代目标部署、ESP32 真机、
声学、弱网或长稳证据。测试过程中使用的临时地址、进程标识、凭据和原始日志不进入 Product 文档。

## 发布门禁

| Gate | 当前状态 | 完成条件 |
| --- | --- | --- |
| Native clean build + size | `build_passed` / `image_sized` | 当前源码已完成 Xtensa rebuild 与 size；发布前仍需在无 `managed_components` 缓存的 clean checkout 重跑，证明依赖恢复可复现 |
| Boot/display/touch | `not_run` | 当前 v2 artifact 已成功烧录并启动到 Wi-Fi；显示、中文字体、touch 与模式 UI 仍需按最终 artifact 人工观察 |
| Wi-Fi/NVS/bootstrap | `device_verified`（Wi-Fi only） | 当前 provisioned primary 网络自动连接通过；稳定公网 bootstrap、grant、保存/重启回读、双网络 fallback 与 credential origin 尚未闭环 |
| WSS voice loop | `not_run` | 稳定公网 bootstrap、`/v2/voice`、ASR final、字幕、LLM、TTS/playout 和 explicit cancel 均待当前 artifact 实测 |
| UDP voice loop | `not_run` | GCM probe、双向媒体、generation、loss/jitter/PLC 和 fallback 通过 |
| AEC/acoustic | `not_run` | 近讲、远讲、double-talk、自回授与 interrupt tail 评测通过 |
| Stability | `not_run` | 20 轮交互和 30 分钟运行，无 panic/WDT、泄漏或持续 underrun |
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
