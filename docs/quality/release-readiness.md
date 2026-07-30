# Release readiness

更新日期：2026-07-30
状态：not ready

本文只记录当前 Product source 的发布门禁。历史迁移 artifact、串口、SSID、临时地址和实验日志不构成当前
release evidence；详细实验材料不进入 Product 仓。

## 当前软件证据

| 范围 | 状态 | 证据 |
| --- | --- | --- |
| Product repository | immutable commit `8583ef3947e85ca2bf3260de3f5dcfea99ab4360` | 当前 Product source 在 `eee6113` 的三个原子提交之上增加 `8583ef3`（默认 media profile Kconfig、host contract tests 与 CI matrix）。根测试 `46 passed`；Server `274 passed, 3 skipped`；Desktop `116 passed, 4 deselected`；Windows deterministic host E2E `4 passed`；Ruff、repository verifier 和 secret scan 通过。Server runtime 仍部署未改 Server 代码的 ancestor `eee6113`；3 个 Redis integration skip 与 4 个 deselected 均不计为通过 |
| `rva-control-v2` schema/fixtures | `contract_verified` | canonical schema、semantic fixtures 与 UDP GCM v2 byte vectors 已由当前根测试覆盖；不替代真实网络/设备闭环 |
| Director grant/fencing | `unit_verified` / `contract_verified` | 完整 Server suite 覆盖 grant、replay、route 与 fencing；真实 Redis 集成 3 项因未配置测试地址而 skipped，不计为通过 |
| Worker `/v2/voice`、RVA runtime | `unit_verified` / `contract_verified` | 完整 Server suite 覆盖 v2 control、response identity、playback facts、cancel ledger、provider 和 teardown 边界 |
| Provider chain canary | standalone `host_verified` / Worker runtime `device_verified` | 隔离的 combined FunASR endpoint 完成精确 WebSocket `ready -> started -> final` probe；完整 canary 为 fixture TTS `1517 ms / 199680 bytes`、ASR `398 ms / 20 chars`、LLM `1124 ms / 20 chars`、response TTS `1455 ms / 192000 bytes`。未记录 transcript/answer 内容。Worker 已原子切换到同一 endpoint，实际 runtime env 与 readiness 已确认；真实设备会话观察到 ASR interim/final、DeepSeek、MiMo 和 playback finished，无 provider error 或 OOM |
| Desktop reference client | commit `eee6113` host verified；distribution `not ready` | unit/contract tests 与 Windows deterministic host E2E 覆盖 `wss-opus-v3`、`udp-opus-gcm-v2`、Opus round trip 和 playback facts；SoundDevice 使用 PortAudio clock + reported latency 的预计 render boundary，真实声卡/DAC/acoustic 仍为 `not_run`。host evidence 不替代目标部署、ESP32、弱网或长稳验证 |
| Worker RVA UDP binding | current firmware `device_verified` / cold default entry `not_ready` | Firmware `8583ef3` 的 UDP variant 完成约 6m50s 会话：6796 uplink packets、20388 decoded/runner frames、43 downlink packets，invalid 0、无 overload；至少两轮 ASR -> LLM -> MiMo -> playback 为 3690/1980 ms，端侧稳态 deadline miss 0、queue drop 0。cold default UDP probe 的决定性复测 3/3 timeout，设备 12 次 send 均成功但 Worker 主机 90 秒抓包为 0，随后安全回退 WSS 并保持稳定 |
| Server static analysis/tests | `host_verified`，full suite 仍有外部集成 skip | commit `eee6113` 的完整 Server suite 为 `274 passed, 3 skipped`，Server Ruff 通过；3 项 skip 不计为通过，真实 Redis gate 仍由独立 CI job 强制执行 |
| Firmware core/session contracts | root/C++ host `host_verified` | 根测试 `46 passed` 覆盖协议级约束；两组 C++ host tests 已在 Linux 原生 `g++` 环境通过；`8583ef3` 另加入 default profile unset/WSS/UDP/conflict contract 与对应 CI matrix |
| WSS protocol/owner | `contract_verified` / parent artifact `device_verified`（basic loop） / current fallback `device_observed` | Firmware parent `eee6113` 完成 bootstrap、`session.opened(wss-opus-v3)`、上行、ASR、LLM、完整 TTS 与 playback finished：150 packets / 447 frames，ASR 4.032 秒音频 / 0.331 秒推理，TTS playback 1530 ms。`8583ef3` 在 cold UDP probe 失败后安全回退 WSS 并保持稳定 |
| Board/audio/UI components | current commit device `partial`；historical diagnostic `partial` | 配置化 variant 已观察联网、LCD、字体、audio codec、AFE、AEC 和 UDP/WSS session；touch 完整交互矩阵与 AEC 声学效果仍未验证，不能由组件初始化或媒体闭环推导通过 |
| Native ESP-IDF composition | Firmware commit `8583ef3` `build_passed` / `image_sized` / device `partial` | ESP-IDF 5.5.2、Xtensa 14.2.0 构建通过；app `2200944` bytes，4 MiB app 分区余 `0x1e6a90`（48%），SHA-256 `2EB804FF10D882602660CE9A913C04EA212CC8799E571CB7B9CC2DC545D06D9F`。bootloader SHA-256 `0275E6BEBDA2FAB766A4D82A1359F179C1989AFFD7921B4029D12529E1520581`，partition table `D6C5D356DECE5D16DB6B87EFB85B62C0D731ACDE8023EBB674F8D8ECD7150B8F`，srmodels `BC56EFEEC122AE9FACDABDB9817BF2F224A8FA7585244B6E351DB84A8AA19088`，font assets `16B19452EAEFA3A6F686BDA60BF459AD64978E3187AC6F04847CB33F6610BD71`。affinity 为 `UNPINNED`，两核 idle Task WDT 均启用，timeout 10 秒 |
| Native WSS/UDP voice loop | WSS basic loop `device_verified`；UDP media loop `device_verified` / cold entry `not_ready` | Server runtime 为 ancestor `eee6113`，Firmware 为 `8583ef3`。UDP 长会话和至少两轮真实 provider/playback 已通过，但 cold default probe 3/3 未到达 Worker 主机；安全回退 WSS 已观察。默认 Product profile 继续使用 WSS，不对未证实的随机端口/NAT 原因做代码修复 |

以上跨进程闭环只证明当前主机上的 Server 拓扑、协议和 provider 数据路径可运行，不替代目标部署、ESP32 真机、
声学、弱网或长稳证据。测试过程中使用的临时地址、进程标识、凭据和原始日志不进入 Product 文档。

当前 `ol` 验证环境已部署 release `rva-20260730T070206Z-eee6113`，Worker identity 为
`worker-ol-20260730T074700Z-eee6113-asr1112`。Director、Worker、coordination、provider network 与 UDP socket readiness 均为
ready，容量配置为 5；部署 archive `rva-eee6113.tar.gz` 的 SHA-256 为
`9f5a892f5c0ce2a0d0fff62a28f4239d35c15f9c05ff1736d25c98c47195a999`。readiness 只证明配置、依赖和网络探测。
独立 combined FunASR endpoint 上的真实 ASR -> LLM -> TTS canary 已通过；Worker runtime 已原子切换到该 endpoint，
实际 env、切换后 readiness 和真实设备 provider chain 均已确认。

历史 commit `314d673` 收敛了三项会造成端云反复断连或第二轮失败的实现问题；这些修复已包含在当前
Product source 中：

- authenticated 但无法解码的单个 Opus 包不再立即升级为 `1002 protocol_error`；单包丢弃，连续 8 个坏包才以
  `1011/media_decode_failed` 关闭，decoder 的其他 `RuntimeError` 仍按 runtime failure 处理。
- VoiceSession 与 runner 不再嵌套竞争相同 deadline；父任务取消时会回收 child，避免遗留 task 和重连期间短暂
  `session_overloaded`。
- WSS 下行 media sequence 统一为按方向、按 session 严格递增；Firmware 不再在每个 `response.begin` 重置 sequence，
  修复第二条 TTS response 首包触发 `wss_media_admission` 的确定性失败。

## 2026-07-28 验证环境增量证据

以下是验证环境的增量证据，不提升正式 release gate。服务端 archive 的 SHA256、不可变 release 名称、实际
`EnvironmentFile` 引用和唯一 Worker incarnation 由受控 release record 保存；不在本仓记录主机地址、路径、
凭据或原始日志。

- Worker 对 DeepSeek-compatible LLM 显式使用 `VOICE_LLM_READ_TIMEOUT_SECONDS`，默认 20 秒。此前 HTTP 200 后
  首 token 超过 SDK 默认 5 秒 read deadline 会形成 `httpx.ReadTimeout`，从而不会调用 TTS；该修复的 Worker
  定向测试 24 项和完整测试 203 项均通过。
- 当前 ESP32 实机在同一采集窗口内两次命中 `wn9s_hiesp`，完成 bootstrap、WSS control、
  `udp-opus-gcm-v2` probe ACK、上行 Opus 和 AFE VAD 状态切换。服务端关联到 FunASR final、LLM、MiMo
  `tts_first_pcm`、`endpoint_playback_started`、`agent_audio_published` 与正常 playback completion；会话由用户
  主动结束，关闭统计存在实际 UDP downlink 包。
- 本轮首音的 `speech_end_to_agent_audio_ms` 为约 3.8 秒和 4.7 秒。样本量仅为两次交互，不能作为延迟 SLA、
  声学/AEC、弱网或 30 分钟稳定性结论。

## 发布门禁

| Gate | 当前状态 | 完成条件 |
| --- | --- | --- |
| Native clean build + size | `passed` | 已从 immutable Product commit `8583ef3` 使用 ESP-IDF 5.5.2 重建，app/bootloader/partition/srmodels/font assets 的 size 与 SHA-256 已记录 |
| Boot/display/touch | current commit `partial` | 配置化五分区 variant 已观察启动、LCD、字体、audio codec、AFE 与 AEC；touch 与完整交互矩阵尚未闭环 |
| Wi-Fi/NVS/bootstrap | current commit `partial` | 配置化 variant 已连接预置网络并完成 bootstrap；Worker 重启窗口的 503/timeout 在 ready 后自动恢复。Wi-Fi flap 与 NVS 保存重启回读仍未执行。首次 fresh worktree 构建遗漏 untracked `sdkconfig.local` 的未配置 artifact 不作为设备结论 |
| Provider chain | `passed`（当前验证环境） | standalone canary、Worker runtime env/readiness 和真实设备会话均确认 ASR -> LLM -> TTS；仍不替代目标 SLA、provider 故障注入或容量结论 |
| WSS voice loop | basic loop `passed` / full gate `partial` | parent artifact 已完成真实 ASR final、LLM、完整 TTS 和 playback，当前 firmware 的安全回退 WSS 已观察；仍须补 explicit cancel 与当前 firmware 的完整多轮，才能关闭完整 gate |
| UDP voice loop | media loop `passed` / cold entry `not_ready` | 当前 firmware 已完成约 6m50s、至少两轮 ASR/LLM/TTS、invalid 0、无 overload；但 cold default probe 决定性复测 3/3 在 Worker 主机前丢失。默认保持 WSS；修复前须先定位公网 UDP ingress/NAT/firewall，不接受未经证据的随机端口改动 |
| AEC/acoustic | `not_run` | 近讲、远讲、double-talk、自回授与 interrupt tail 评测通过 |
| Wake word | historical diagnostic `device_verified` / current commit `not_run` | 旧 artifact 的 `wn9s_hiesp` 命中和 audio owner handoff 保留为历史；当前 commit 仍需重复唤醒/退出和异常停止回归 |
| Stability | current UDP 6m50s + WSS short observation `partial` / release gate `not ready` | UDP 长会话端侧稳态 deadline miss 0、queue drop 0，服务端 invalid 0、无 overload；WSS 10 秒窗口 max media age 约 22.5 ms、无 Task WDT。当前 commit 尚未执行 20 轮或 30 分钟 HIL |
| Desktop distribution | `not ready` | 尚未按目标 OS/architecture/artifact 完成 PyAV、FFmpeg、libopus、sounddevice、PortAudio 及传递 native libraries 的分发许可证、notice/适用义务和 SBOM 核查；lockfile 只固定 Python distribution，不能替代 native binary provenance 与构建选项清单 |
| Security/repository | `not ready` | repository verifier、secret scan、根测试、protocol 测试和 `git diff --check` 通过；`xiaozhi-fonts` 固定包只声明 MIT metadata、未附上游许可证文本，发布前必须完成许可证来源复核 |

## Clean-slate v2 边界

Current runtime 不保留 RVA v1/Xiaozhi dual stack。Canonical `udp-opus-gcm-v2` byte fixtures 位于
`protocol/udp_opus_gcm_v2/`；旧 route、profile 或 wire version 必须 fail closed。历史 migration provenance
只用于来源追溯，不参与构建、部署或发布验收。

## 2026-07-22 v1 artifact 的历史 HIL 结论

以下事实用于解释硬件和资源基线，不构成当前 v2 artifact 的 release evidence：

- `esp_audio_codec` 的 Opus encode 在本配置下首帧累计使用约 26 KiB task stack。历史 24 KiB 配置发生
  `InstrFetchProhibited`；该 v1 artifact 的 legacy 单体 uplink task 当时配置为 36 KiB，按该次 HIL 数据保留约
  10 KiB 余量。当前拆分后的 framer、encoder、sender task 不能继承该整体余量结论，仍须在最终 artifact 的完整
  ASR/TTS 交互和长稳测试中分别采样 high-water mark。
- Worker 的 10 秒 WebSocket PING 会作为零长度 `WEBSOCKET_EVENT_DATA` 到达 ESP-IDF callback，同时由组件自动
  回复 PONG。Transport adapter 必须忽略 PING/PONG/CLOSE data callback，并只通过 CLOSED/DISCONNECTED 事件驱动
  session teardown；否则会稳定形成约 10 秒断连。
- 以上诊断用 guard、逐帧 watermark 和首次 encode 日志已从最终镜像删除；容量修复、stack baseline、Opus frame
  contract 和 control-frame admission 保留。原始串口与服务日志属于 ignored HIL artifact，不进入发布物。
