# 兼容性基线

快照日期：2026-07-20
用途：迁移比较，不是最终发布验收

## 1. 固定来源

| 项 | 基线 |
| --- | --- |
| ESP32 upstream | `xiaozhi-esp32@7b190b78e4f8dfef14126f6cd478c134b3cd3cd8` |
| Board/target | `lichuang-dev` / `esp32s3` |
| ESP-IDF | `v5.5.2`，revision `30aaf64524299d3bde422ca9a2848090d1bc5d0f` |
| Dependency lock | `firmware/locks/xiaozhi-esp32.dependencies.lock` |
| Control | `xiaozhi-control-v1` |
| Media | `wss-opus-v1` baseline；`udp-opus-gcm-v1` challenger |
| Runtime | roomless LiveKit `AgentSession` |

外部来源与许可证以 `third_party/sources.lock.yaml` 为准。External checkout、build、secret 和固件 binary 不进入 Git。

## 2. 来源研究仓证据

### 2.1 旧 WSS HIL baseline

固定 upstream + overlay `0001..0005` 曾完成 app-only 烧录、cold boot、Wi-Fi、真实 provider ASR/LLM/TTS
闭环、约 10 分钟 listening smoke、长回复播放回归和一次自动 double-talk/打断。该证据证明当时 artifact、
Server、网络和 provider 环境可以闭环，仅作为迁移对照。

它不证明：

- 新仓 Server/Director/Worker 组合可用。
- 最终 overlay `0001..0010` 可启动或连接。
- UDP profile、弱网收益、声学质量或生产并发。

### 2.2 最终 overlay 来源构建

来源工作区的 `0001..0010` 已通过 canonical fixture、source/lifecycle contract、patch round-trip 和 ESP-IDF
5.5.2 clean build。迁移基线与后续 A004 验证记录了两个未提交 artifact：

| Evidence | Value |
| --- | --- |
| Migration fresh build SHA-256 | `870dfb4351281b1d403b0948566d524a5935af71a86d0ce3fdfffea0348715e3` |
| A004 result | `build_passed`，2215/2215，exit 0 |
| A004 `xiaozhi.bin` bytes | 2,958,944 |
| A004 SHA-256 | `5A4B2AF3EDE8D7540515E53D03B93F1E83A56782E1E74375C5C098863DA7B31E` |
| A004 app partition free | `0x11d9a0`，约 28% |
| A004 DIRAM | 170,887 / 341,760 bytes，50.0% |

这是来源构建身份。新仓随后已独立 materialize 并重新构建，结果见 3 节。三个 clean build hash 不同，当前不声明
bit-level reproducibility；差异包含 ignored local configuration。来源研究仓的两个 artifact 未在本轮烧录；新仓
SHA-256 `43bac4d4ed678b3298cc9f4c8e9da0c4ab7608af731406cec31939ee457350c8` 的 final reference artifact 已烧录，
不得混淆三者身份。

### 2.3 Protocol baseline

- Control JSON schema/positive/negative fixtures 已固定。
- UDP 32-byte header、AES-GCM positive/negative vectors 和 non-zero KEEPALIVE timestamp 已有跨语言 host contract。
- ESP32 A004 lifecycle hardening 已进入 overlay，包括 revocation、connection generation、owner 隔离和有界 stop。

这些证据等级为 `contract_verified/host_verified`，不等于 UDP 实链路 `device_verified`。

## 3. 新仓当前证据边界

新仓已选择性提取 canonical protocol、firmware reference overlay、source lock、Server 和 repository contract。
当前精确实现身份为 Server commit `fca8de8` + repairs `259aeee`/`d2fa0ca`、firmware/repro commit `cf9bc69`。以下状态只
采用新仓本轮证据：

| 验证 | 状态 |
| --- | --- |
| 新仓 repository/protocol contract | 根 pytest `15 passed` |
| 新仓 Server unit/contract/integration | Server Ruff 通过；Redis-enabled pytest `179 passed` |
| Shared coordination | atomic lease/fencing、heartbeat/drain、`jti` 单次消费及 Redis 重建/跨实例 replay 拒绝已测试 |
| 新仓真实进程 host media E2E | synthetic Chinese 经 Director grant、WSS + UDP Opus/GCM 到真实 providers 并产生 downlink audio |
| Host provider 单次观测 | FunASR final/STT 约 480 ms；DeepSeek HTTP 200/TTFT 约 9876 ms；CosyVoice HTTP 200/TTFB 约 594 ms |
| 新仓 firmware clean build/size | `build_passed/image_sized`；2215/2215，image `0x2d2660`，余量 28%，DIRAM 50.0% |
| 新仓 firmware artifact SHA-256 | `43bac4d4ed678b3298cc9f4c8e9da0c4ab7608af731406cec31939ee457350c8`；含 ignored local config，不发布 |
| 最终 firmware flash/cold boot | COM11 ESP32-S3 rev0.2；完整分区 hash verified；`boot_observed` |
| Boot/network 初始化 | 8 MB PSRAM，Wi-Fi `192.168.1.105`；display/audio/ES7210/AEC/VAD/wake model 初始化；无 panic/WDT |
| 最终 WSS device path | “你好小智”唤醒成功；handshake 约 20 ms；持续 WSS media 与真机 ASR/TTS `not_run` |
| 最终 UDP device path | GCM probe first-attempt ready；600+ UDP Opus uplink packets；真机 ASR/downlink/playout `not_run` |
| UI/触摸人工视觉与配置回读 | `not_run`；display init 与 Wi-Fi 连接不能替代人工验收 |
| 近讲/远讲/double-talk AEC | `not_run` |
| WSS/UDP 同条件弱网 A/B | `not_run` |
| 20 轮真人交互 | `not_run` |
| 30 分钟长稳 | `not_run` |
| Director + Redis coordination semantics | `unit_verified`；真实多进程容量、HA/网络分区故障演练 `not_run` |

本轮 launcher 使用 ignored `.env` 启动 Director `0.0.0.0:8079`、Worker `0.0.0.0:8080`、UDP
`0.0.0.0:8092`，Redis/provider TCP readiness 与 lifecycle repair `d2fa0ca` stop/start 通过。Host synthetic media 已覆盖
FunASR、LLM、CosyVoice 和 downlink audio；字幕/打断未单独验收，且该结果不能替代真机声学输入。

设备自动播放测试语句时采集 peak 偏低，虽持续发送 600+ UDP Opus uplink packets，仍未触发真机 ASR。该结果是明确的
未完成而非 provider 失败；真机 ASR/TTS 必须在可控声源/真人输入下重测。

## 4. 四象限门禁

每个改变 wire、lifecycle、provider adapter 或 firmware owner 的批次，WSS 与 UDP 分开验证：

| Endpoint | Server | 目的 |
| --- | --- | --- |
| reference | reference | 环境与旧 baseline 可重现 |
| new | reference | 新固件未破坏旧 Server contract |
| reference | new | 新 Server 未破坏旧固件 contract |
| new | new | 最终组合与新增 observability |

Reference 表示固定 artifact/source 和 redacted config shape，不表示“当前目录随意 checkout”。测试记录必须包含
source revision、overlay digest、Server commit、firmware SHA-256、profile、网络、provider 和时间。

## 5. 行为冻结清单

- Boot/display/touch/codec/PA 无 panic、WDT 或黑屏。
- Wi-Fi 扫描、输入、NVS 优先级、endpoint/token origin 绑定。
- 16 kHz uplink、24 kHz downlink、60 ms Opus cadence、播放音量和 AEC reference。
- 触摸/唤醒、listen、ASR/TTS 字幕、长回复、abort、generation 和 fresh reconnect。
- WSS hello/header/binary framing/close。
- UDP grant/probe/GCM/replay/source/reorder/PLC/expiry。
- FunASR/LLM/TTS 请求形状、streaming、timeout、cancellation。
- latency timeline、underrun、queue/media age、heap/stack/CPU。

基线变化必须在 release evidence 中说明原因，不能静默更新此文档来掩盖回退。
