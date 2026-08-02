# Release readiness

更新日期：2026-08-02
状态：not ready

本文只记录当前 Product 工作树和可复核的发布门禁。历史 artifact、串口、SSID、临时地址、实验日志和旧协议结论
不构成当前 release evidence；它们由 Research 或 Git 历史保存。

## 当前软件证据

当前候选位于 `codex/rva-protocol-identity-reset`，代码 identity 为
`1408228759e931fe4e5047ec74d40deed93e58a9`。该提交包含 Protocol Identity Reset、长 TTS 有界分块和 UDP 停止竞态
日志修复；本文件作为后续证据提交记录该代码 identity。分支尚未合并或打 tag，因此不是正式 release。

| 范围 | 当前状态 | 本轮实际结果与边界 |
| --- | --- | --- |
| Product repository | `host_verified` | root Ruff 与 repository tests：`37 passed`；repository verifier、tracked/untracked secret scan 和 `git diff --check` 通过 |
| Server | `host_verified` | `286 passed, 3 skipped`；3 项本机环境 skip 不计为通过。长 TTS 分块 focused/受影响测试包含在该 suite 中 |
| Desktop reference | `host_verified` | 非 host suite `116 passed`，确定性 WSS/UDP host E2E `4 passed` |
| Native runtime/WSS/headless contracts | `host_verified` | device config、voice terminal、native runtime、WSS、headless 和 component-boundary contracts 通过 |
| Native UDP/GCM contracts | `host_verified` | C++20 session/playout contracts、pinned ESP-IDF source verifier、Mbed TLS GCM positive/negative fixtures 通过 |
| Native ESP-IDF composition | `build_passed/image_sized` | ESP-IDF `v5.5.2@30aaf645...` 构建当前工作树：app `0x21a190`，4 MiB app 分区剩余 `0x1e5e70`（47%），静态 D/IRAM `202751/341760`，IRAM `16384/16384`；local binary SHA-256 `253517289787e3e6eb0d0d35116a6f45ee15c4fcc066726be125b2d6a107e3d8` |

Host/软件测试和 build 不证明 ESP32 真机、真实 provider、公网 TLS、声学、弱网、资源余量、长稳或正式部署。

## 2026-08-02 公网真机回归

本轮使用立创实战派 ESP32-S3、`COM11@115200`、公网 `182.254.219.7` 和 release
`rva-20260801T165656Z-output-chunk`。Director/Worker 均运行于 Linux `systemd --user`，Worker identity 为
`worker-ol-20260801T165656Z-output-chunk`。完整回归先在同目录前一候选执行；最后又将 SHA-256
`253517289787e3e6eb0d0d35116a6f45ee15c4fcc066726be125b2d6a107e3d8` 的当前 image 写入 bootloader、app、ESP-SR
model 和字体分区，在保留 NVS 的前提下补做 UDP bootstrap、双向媒体、完整 TTS 和 MIC normal close。

| 场景 | 结果 | 观察边界 |
| --- | --- | --- |
| Boot/UI/Wi-Fi/bootstrap | `device_verified` | 重启后显示、WakeNet、默认 Wi-Fi、Director bootstrap 和 profile 选择正常 |
| WSS | `device_verified` | 多轮短问、长答、上下文、中文数字、完整 TTS、MIC stop、新 session 重连均通过 |
| WSS interruption | `device_verified` | “别说了”快速停止且旧 generation 未恢复；普通短语未误打断，约 35.82 秒 TTS 自然结束 |
| UDP admission/media | `device_verified` | 选择 `udp-opus-gcm/1`，authenticated probe 首次成功；多轮 ASR/LLM/TTS 无 WSS 隐式回退，媒体队列无堆积 |
| UDP interruption | `device_verified` | 服务端记录 `response_generation_changed` fence 和 `endpoint_playback_finished.interrupted=true`，旧 segment 被拒绝 |
| UDP grant refresh | `device_verified` | monotonic refresh 到期后正常 release、fresh bootstrap、新 probe 首次成功；续租后完整 ASR/LLM/TTS |
| UDP MIC stop | `device_verified` | UI 回待机；Worker `close_code=1000/close_reason=normal`，Director route release `200` |
| Long TTS regression | `device_verified` | 修复 30 秒硬上限后连续约 100.17 秒 TTS 完整流畅；无 BufferError、queue drop 或 generation 残留 |

端侧持续上行观察中队列保持 `1/1`、drops `0/0`，未见 WDT、panic、stale 或非预期重连。MIC stop 时曾把预期中的
UDP send 取消记录为 `udp_uplink_send` warning；当前源码已用 running-state guard 修复并通过 host contract。最终 hash
image 已完成正常 MIC close，但本次串口采集在会话中段被强制停止，未保留 stop 时刻原始行，因此“warning 消失”仍只记
`contract_verified`，不把缺失日志写成实机通过。

## 当前发布门禁

| Gate | 当前状态 | 完成条件 |
| --- | --- | --- |
| Native clean build + size | `passed` | 当前工作树使用锁定 ESP-IDF 完成 build/size 并记录 local binary hash；commit 后仍需 CI clean rebuild 和 artifact provenance |
| Current image flash/boot | `device_verified` | 当前 SHA-256 image 完整写入并通过 esptool 写后 hash 校验；启动后 Wi-Fi/AFE 正常，无 panic/WDT |
| Boot/display/touch | `device_verified` | 当前 image 完成启动、显示、WakeNet、UDP 模式选择和 MIC 交互；完整 UI 矩阵来自同目录前一候选 |
| Wi-Fi/NVS/bootstrap | `device_verified` | 当前 image 保留 NVS 后自动联网、Director bootstrap `200`、UDP probe 首次成功；Wi-Fi flap 未专项执行 |
| Provider chain on current image | `device_verified` | 当前 image 通过公网真实 ASR -> LLM -> TTS，服务端记录两次完整 playback finished |
| WSS voice loop | `device_verified` | 同目录前一候选完成多轮、长 TTS、正/负向打断、MIC stop 和重连；最后日志修复不触及 WSS 路径 |
| UDP voice loop | `device_verified` | 当前 image 完成 authenticated probe、双向媒体和 normal close；前一候选另完成 generation fence 与 grant refresh |
| End-to-end latency | `not_run` | 按固定样本量采集端云 trace，报告口径、分位数和原始证据 |
| AEC/acoustic | `not_run` | 当前 image 完成近讲、远讲、double-talk、自回授和 interrupt tail 评测 |
| Stability | `incomplete` | 本轮持续会话跨 grant refresh 且无 WDT/drop/stale；仍未把固定 30 分钟、20 轮和 2 小时 soak 绑定到 durable artifact |
| Desktop distribution | `not ready` | 目标 OS/architecture 的 native audio/codec 许可证、notice、SBOM 和 provenance 完成 |
| Security/repository | `incomplete` | 代码、协议、secret、许可证文件和 CI 门禁通过；许可证法律复核、SBOM、发布归档和正式 main/release 记录仍未完成 |

## 运行时边界

当前 registry/runtime 只接受 `rva/1`、`wss-opus/1` 和 `udp-opus-gcm/1`。不支持的 path、control 或 media
profile 必须 fail closed；旧实现不进入构建、部署或发布验收。Server 生产运行使用 Linux/container，Windows 原生
launcher 和 Job Object 生命周期不属于 Product 支持面。

## 证据规则

- `host_verified` 只表示本机确定性 contract/lifecycle 通过。
- `build_passed`、`image_sized`、`device_verified`、`acoustic_verified`、`public_path_verified` 和 `measured` 必须
  绑定同一个 durable source/image/config identity；不能从旧版本或历史日志继承。
- 未运行的 Redis、ESP-IDF、provider、公网、设备、声学、弱网和长稳项目写 `not_run`，不能用 skipped 或旧结果替代。
- 每次部署应另建 release record，记录完整 Product commit、archive/image digest、配置 identity、Worker incarnation 和
  实际验证范围；secret、原始音频和完整 provider body 不进入 Product。
