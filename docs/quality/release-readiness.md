# Release readiness

更新日期：2026-07-22
状态：not ready

本文只记录当前 Product source 的发布门禁。历史迁移 artifact、串口、SSID、临时地址和实验日志不构成当前
release evidence；详细实验材料不进入 Product 仓。

## 当前软件证据

| 范围 | 状态 | 证据 |
| --- | --- | --- |
| Product repository | `repository_verified` | 根 suite 42 项（其中 protocol 9 项）、repository verifier、secret scan 和 `git diff --check` 通过 |
| `rva-control-v1` schema/fixtures | `contract_verified` | 根协议 suite 9 项通过 |
| Director 双 binding、grant/fencing | `unit_verified` | 当前源码完整 Server suite 251 项通过；3 项 Redis 环境测试显式 skip，不记为通过 |
| Worker `/v1/voice`、RVA runtime | `process_verified` | 当前源码完整 Server suite 与本机独立进程 Director/Worker 真实 provider WSS 闭环通过，收到 166 个下行媒体包 |
| Worker RVA UDP binding | `process_verified` | focused grant/probe/GCM/双向媒体测试及本机独立进程真实 provider UDP GCM 闭环通过，收到 119 个下行媒体包 |
| Server static analysis | `verified` | Ruff 检查通过 |
| Firmware core/session contracts | `host_verified` | headless contract 与 canonical UDP fixture boundary 通过 |
| WSS protocol/owner | `host_verified` | strict parser、fragment、queue、teardown host tests 通过 |
| Board/audio/UI components | `host_verified` | focused host/Xtensa component compile；不等于 HIL |
| Native ESP-IDF composition | `build_passed` / `image_sized` | ESP-IDF 5.5.2、`esp-14.2.0_20251107` 构建通过；镜像 `0x197d90` bytes，4 MiB app 分区剩余 `0x268270` bytes（约 60%）；SHA-256 `AF3E42F3EED54D60C47C3C08D84083E2BF539E7E37097D4FC7DECCB9E5FED060` |
| Native WSS uplink | `device_verified` | 当前镜像在目标 ESP32-S3 上完成 Wi-Fi、Director bootstrap、Worker admission、AFE 与 Opus uplink，连接连续超过 70 秒并跨过多个 10 秒 WebSocket PING 和 45 秒 idle 门限；未出现 panic、重连或 media idle |

以上跨进程闭环只证明当前主机上的 Server 拓扑、协议和 provider 数据路径可运行，不替代目标部署、ESP32 真机、
声学、弱网或长稳证据。测试过程中使用的临时地址、进程标识、凭据和原始日志不进入 Product 文档。

## 发布门禁

| Gate | 当前状态 | 完成条件 |
| --- | --- | --- |
| Native clean build + size | `build_passed` / `image_sized` | 当前源码已完成 Xtensa rebuild 与 size；发布前仍需在无 `managed_components` 缓存的 clean checkout 重跑，证明依赖恢复可复现 |
| Boot/display/touch | `not_run` | 当前 artifact 已由服务端连接证明启动；中文 UI、开始/停止和模式切换仍需人工观察 |
| Wi-Fi/NVS/bootstrap | `device_verified`（部分） | 当前 provisioned primary 网络、bootstrap 和 grant 已通过；保存/重启回读、双网络 fallback 与 credential origin 仍需覆盖 |
| WSS voice loop | `device_verified`（transport only） | 真机 AFE、Opus uplink和长于 idle 门限的 WSS 会话已通过；ASR final、字幕、LLM、TTS/playout 和点击/近讲打断仍未完成 |
| UDP voice loop | `not_run` | GCM probe、双向媒体、generation、loss/jitter/PLC 和 fallback 通过 |
| AEC/acoustic | `not_run` | 近讲、远讲、double-talk、自回授与 interrupt tail 评测通过 |
| Stability | `not_run` | 20 轮交互和 30 分钟运行，无 panic/WDT、泄漏或持续 underrun |
| Security/repository | `verified` | repository verifier 已自动检查字体 copyright notice 与 OFL/MIT 许可证文件；secret scan、根测试、protocol 测试和 `git diff --check` 通过；发布前仍需对最终提交 identity 重跑 |

## 兼容线退役条件

只有 native endpoint 在同一设备与 provider 范围达到上述门禁后，才能删除 Xiaozhi firmware target 与 legacy
Server binding。`firmware/device` headless contract harness 已只消费正式 components，不是兼容线退役前置项。
Canonical `udp-opus-gcm-v1` byte fixtures 位于 `protocol/udp_opus_gcm_v1/`，任何 wire 修改必须通过双端合同与
兼容测试。

## 2026-07-22 native HIL 结论

- `esp_audio_codec` 的 Opus encode 在本配置下瞬时使用约 20 KiB task stack。24 KiB uplink task 在进入 encode
  前仅剩约 18 KiB，导致返回上下文被 PCM 数据覆盖并触发 `InstrFetchProhibited`；当前使用组件 all-encoder
  test 的 40 KiB baseline，实测最低余量约 14 KiB。
- Worker 的 10 秒 WebSocket PING 会作为零长度 `WEBSOCKET_EVENT_DATA` 到达 ESP-IDF callback，同时由组件自动
  回复 PONG。Transport adapter 必须忽略 PING/PONG/CLOSE data callback，并只通过 CLOSED/DISCONNECTED 事件驱动
  session teardown；否则会稳定形成约 10 秒断连。
- 以上诊断用 guard、逐帧 watermark 和首次 encode 日志已从最终镜像删除；容量修复、stack baseline、Opus frame
  contract 和 control-frame admission 保留。原始串口与服务日志属于 ignored HIL artifact，不进入发布物。
