# Release readiness

更新日期：2026-07-21
状态：not ready

本文只记录当前 Product source 的发布门禁。历史迁移 artifact、串口、SSID、临时地址和实验日志不构成当前
release evidence；详细实验材料不进入 Product 仓。

## 当前软件证据

| 范围 | 状态 | 证据 |
| --- | --- | --- |
| Product repository | `repository_verified` | 根测试 36 项、protocol 测试 9 项、repository verifier、secret scan 和 `git diff --check` 通过 |
| `rva-control-v1` schema/fixtures | `contract_verified` | 根协议 suite 9 项通过 |
| Director 双 binding、grant/fencing | `unit_verified` | 当前源码完整 Server suite 241 项通过；3 项 Redis 环境测试显式 skip，不记为通过 |
| Worker `/v1/voice`、RVA runtime | `process_verified` | 当前源码完整 Server suite 与本机独立进程 Director/Worker 真实 provider WSS 闭环通过，收到 166 个下行媒体包 |
| Worker RVA UDP binding | `process_verified` | focused grant/probe/GCM/双向媒体测试及本机独立进程真实 provider UDP GCM 闭环通过，收到 119 个下行媒体包 |
| Server static analysis | `verified` | Ruff 检查通过 |
| Firmware core/session contracts | `host_verified` | headless contract 与 canonical UDP fixture boundary 通过 |
| WSS protocol/owner | `host_verified` | strict parser、fragment、queue、teardown host tests 通过 |
| Board/audio/UI components | `host_verified` | focused host/Xtensa component compile；不等于 HIL |
| Native ESP-IDF composition | `build_passed` / `image_sized` | ESP-IDF 5.5.2、`esp-14.2.0_20251107` 构建通过；镜像 `0x20c4c0` bytes，4 MiB app 分区剩余 `0x1f3b40` bytes（约 49%）；SHA-256 `275F5ADD14511C1AEFC85D8A132D9E0AEBB9E1E9028106A4CCC614150F81E873` |

以上跨进程闭环只证明当前主机上的 Server 拓扑、协议和 provider 数据路径可运行，不替代目标部署、ESP32 真机、
声学、弱网或长稳证据。测试过程中使用的临时地址、进程标识、凭据和原始日志不进入 Product 文档。

## 发布门禁

| Gate | 当前状态 | 完成条件 |
| --- | --- | --- |
| Native clean build + size | `build_passed` / `image_sized` | 当前源码已完成 Xtensa rebuild 与 size；发布前仍需在无 `managed_components` 缓存的 clean checkout 重跑，证明依赖恢复可复现 |
| Boot/display/touch | `not_run` | 当前 artifact 冷启动、中文 UI、开始/停止和模式切换通过 |
| Wi-Fi/NVS/bootstrap | `not_run` | 保存、重启回读、双网络 fallback、credential origin 和 grant 通过 |
| WSS voice loop | `not_run` | 真机 ASR、字幕、LLM、TTS/playout 和点击/近讲打断通过 |
| UDP voice loop | `not_run` | GCM probe、双向媒体、generation、loss/jitter/PLC 和 fallback 通过 |
| AEC/acoustic | `not_run` | 近讲、远讲、double-talk、自回授与 interrupt tail 评测通过 |
| Stability | `not_run` | 20 轮交互和 30 分钟运行，无 panic/WDT、泄漏或持续 underrun |
| Security/repository | `verified` | repository verifier、secret scan、根测试、protocol 测试和 `git diff --check` 通过；发布前仍需对最终提交 identity 重跑 |

## 兼容线退役条件

只有 native endpoint 在同一设备与 provider 范围达到上述门禁后，才能删除 Xiaozhi firmware target 与 legacy
Server binding。`firmware/device` headless contract harness 已只消费正式 components，不是兼容线退役前置项。
Canonical `udp-opus-gcm-v1` byte fixtures 在迁移到中性目录并通过双端兼容测试前不得删除。
