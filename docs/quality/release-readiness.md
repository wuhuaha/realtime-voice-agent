# Release readiness

更新日期：2026-08-01
状态：not ready

本文只记录当前 Product 工作树和可复核的发布门禁。历史 artifact、串口、SSID、临时地址、实验日志和旧协议结论
不构成当前 release evidence；它们由 Research 或 Git 历史保存。

## 当前软件证据

当前分支基于 `main@32d65b1`，运行代码候选为 `da43d1b`；其代码与固件构建证据来自 GitHub Actions run
`30690783808`。后续提交只更新发布证据文档，docs-only 验证 run `30691383849`、`30691662704` 均为 7/7 jobs
成功。该分支尚未合并到 `main`，也不是正式 release。

| 范围 | 当前状态 | 本轮实际结果与边界 |
| --- | --- | --- |
| Product repository | `host_verified` | root Ruff 与 repository tests：`37 passed`；repository verifier、secret scan 和 `git diff --check` 通过；CI 同样通过 |
| Server | `host_verified` | 本地 suite `286 passed, 3 skipped`；CI Server 与 Redis coordination、双 Worker subprocess E2E 均通过，3 项本机 skip 不计为通过 |
| Desktop reference | `host_verified` | 本地非 host suite `116 passed`、host `4 passed`；CI Linux Desktop 与 host E2E 均通过 |
| Native runtime/WSS/headless contracts | `host_verified` | 本机 native runtime、WSS、headless 通过；CI pinned ESP-IDF host contracts 通过 |
| Native UDP/GCM contracts | `host_verified` | 本机 C++ contract 通过；CI pinned ESP-IDF source verifier、UDP C++ 和 GCM positive/negative fixtures 通过 |
| Native ESP-IDF composition | `build_passed/image_sized` | CI run `30690783808` 使用 pinned ESP-IDF 构建代码候选 `da43d1b`：app `0x219d70`，4 MiB 分区剩余 `0x1e6290`（47%），DIRAM `202751/341760`，IRAM `16384/16384`；后续 docs-only run `30691383849`、`30691662704` 也重新通过 build/size job；CI 未保留可发布 binary hash |

Host/软件测试和 CI build 不证明 ESP32 真机、真实 provider、公网 TLS、声学、弱网、资源余量、长稳或正式部署。

## 当前发布门禁

| Gate | 当前状态 | 完成条件 |
| --- | --- | --- |
| Native clean build + size | `passed` | 代码候选 `da43d1b` 的 CI run `30690783808` 以及 docs-only run `30691383849`、`30691662704` 均使用锁定 ESP-IDF 完成构建、size、分区与资源检查；发布前仍需归档 binary/hash |
| Current image flash/boot | `not_run` | 将同源 image 烧录目标板并保存可关联的启动证据 |
| Boot/display/touch | `not_run` | 当前 image 完成启动、显示、触摸和最小交互矩阵 |
| Wi-Fi/NVS/bootstrap | `not_run` | 当前 image 完成首次联网、持久化回读、Director bootstrap、重连和 Wi-Fi flap |
| Provider chain on current image | `not_run` | 当前 image 完成 ASR -> LLM -> TTS 的可追溯真机闭环 |
| WSS voice loop | `not_run` | 当前 image 完成多轮上行、ASR final、完整 TTS、playback、explicit cancel 和重连 |
| UDP voice loop | `not_run` | host/GCM contract 已通过；仍需同源 image 完成 authenticated probe、双向媒体、expiry/fresh reconnect、模式选择和异常恢复 |
| End-to-end latency | `not_run` | 按固定样本量采集端云 trace，报告口径、分位数和原始证据 |
| AEC/acoustic | `not_run` | 当前 image 完成近讲、远讲、double-talk、自回授和 interrupt tail 评测 |
| Stability | `incomplete` | 当前 image 完成多轮、持续运行、queue/deadline/disconnect/WDT/资源水位门禁 |
| Desktop distribution | `not ready` | 目标 OS/architecture 的 native audio/codec 许可证、notice、SBOM 和 provenance 完成 |
| Security/repository | `incomplete` | 代码、协议、secret、许可证文件和 CI 门禁通过；许可证法律复核、SBOM、发布归档和正式 main/release 记录仍未完成 |

## 运行时边界

当前 registry/runtime 只接受 `rva-control-v2`、`wss-opus-v3` 和 `udp-opus-gcm-v2`。不支持的 path、control 或 media
profile 必须 fail closed；旧实现不进入构建、部署或发布验收。Server 生产运行使用 Linux/container，Windows 原生
launcher 和 Job Object 生命周期不属于 Product 支持面。

## 证据规则

- `host_verified` 只表示本机确定性 contract/lifecycle 通过。
- `build_passed`、`image_sized`、`device_verified`、`acoustic_verified`、`public_path_verified` 和 `measured` 必须
  绑定同一个 durable source/image/config identity；不能从旧版本或历史日志继承。
- 未运行的 Redis、ESP-IDF、provider、公网、设备、声学、弱网和长稳项目写 `not_run`，不能用 skipped 或旧结果替代。
- 每次部署应另建 release record，记录完整 Product commit、archive/image digest、配置 identity、Worker incarnation 和
  实际验证范围；secret、原始音频和完整 provider body 不进入 Product。
