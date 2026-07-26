# Release readiness

更新日期：2026-07-26
状态：not ready

本文只记录当前 Product source 的发布门禁。历史迁移 artifact、串口、SSID、临时地址和实验日志不构成当前
release evidence；详细实验材料不进入 Product 仓。

## 当前软件证据

| 范围 | 状态 | 证据 |
| --- | --- | --- |
| Product repository | source HEAD `6059b5b7d39e67068fdb80ddc459c770e39661cd` + reviewed dirty candidate；last stable Server `314d6734a306c2735eeefe2ae69b819f8c70a981` | 本轮 Server Ruff 与 `234 passed, 3 skipped`、repository Ruff、secret scan 与根测试 `46 passed` 均通过；dirty candidate 尚须提交并以 clean source 重建 |
| `rva-control-v2` schema/fixtures | `contract_verified` | canonical schema、semantic fixtures 与 UDP GCM v2 byte vectors 已由当前根测试覆盖；不替代真实网络/设备闭环 |
| Director grant/fencing | `unit_verified` / `contract_verified` | 完整 Server suite 覆盖 grant、replay、route 与 fencing；真实 Redis 集成 3 项因未配置测试地址而 skipped，不计为通过 |
| Worker `/v2/voice`、RVA runtime | `unit_verified` / `contract_verified` | 完整 Server suite 覆盖 v2 control、response identity、playback facts、cancel ledger、provider 和 teardown 边界 |
| Worker RVA UDP binding | `contract_verified` / `device_verified`（2026-07-26 HIL） | authenticated probe 12 ms；连续两轮完整播放 8190/9360 ms；12m29s 长稳跨一次 fresh lease/epoch/key，旧会话 9902 包、29706 解码/推送帧、queue=0、invalid_opus=0，新会话 probe 22 ms 后继续收包 |
| Server static analysis/tests | `verified` | 本轮 Server Ruff 与完整 suite `234 passed, 3 skipped`，0 failed；3 个 Redis 环境 skip 仍不计为集成通过。Windows 子进程退出确认用例此前曾偶发 5 秒超时，本轮复跑通过，作为稳定性关注项保留 |
| Firmware core/session contracts | `contract_verified` / host tests `not_run` | 根测试覆盖协议级约束；本机缺 host compiler，C/C++ host contract test 未执行。本轮修复了 disabled Kconfig bool 被误判为 auto-start 的确定性错误 |
| WSS protocol/owner | `contract_verified` / current device `not_run` | 下行 media sequence 按方向、按 session 严格单调；当前 MIC-owned artifact 尚未完成物理模式切换与 WSS 双轮真机回归 |
| Board/audio/UI components | `device_verified` / interaction `partial` | 诊断 artifact 已观察 LCD/LVGL、Qwen 字体、touch driver、双 Wi-Fi fallback、DHCP 与 WakeNet 命中；MIC/mode/provisioning 的完整触摸矩阵仍未执行 |
| Native ESP-IDF composition | current source `build_passed` / `image_sized`；previous diagnostic artifact `flashed` | 锁定 ESP-IDF 5.5.2 revision `30aaf645...` 构建通过；current candidate app `0x2160f0`、SHA256 `8a49f23fcfcc4d3d339a9ef1f43ea11439ae410da9aa1914dbacea761e1b53cf`、4 MiB app 分区余量 48%。该 candidate 已移除一次性 PCM instrumentation，尚未烧录 |
| Native WSS/UDP voice loop | diagnostic UDP artifact `device_verified` / WSS `not_run` | 唤醒后连续多轮观察到 `user_speech_started/ended`、FunASR final、LLM、MiMo、完整 playback；用户确认 ASR/TTS 正常。current clean candidate 尚需烧录复核 |

以上跨进程闭环只证明当前主机上的 Server 拓扑、协议和 provider 数据路径可运行，不替代目标部署、ESP32 真机、
声学、弱网或长稳证据。测试过程中使用的临时地址、进程标识、凭据和原始日志不进入 Product 文档。

当前 `ol` 验证环境已部署
`/home/ubuntu/services/realtime-voice-agent/releases/314d6734a306c2735eeefe2ae69b819f8c70a981`，实际
`EnvironmentFile` 使用 `VOICE_WORKER_ID=worker-ol-314d673`；Director/Worker readiness 和 UDP ready 已通过。
部署 archive 为 `rva-314d673.tar.gz`，SHA256
`2b8483b654d5c6cab6bc727a7547a8436fb8888188786327fecfd70fccb47ca4`。部署 identity 与 readiness 本身不替代
上表单独记录的 ESP32 HIL，也不证明 WSS、声学、弱网或正式 release gate 已通过。

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
| Native clean build + size | dirty candidate `build_passed` / `image_sized` | 当前 source 已完成 Xtensa build、size 与 artifact hash；提交后仍需 clean source rebuild，证明依赖恢复和制品身份可复现 |
| Boot/display/touch | `partial` | 当前 artifact 启动、LCD/LVGL、Qwen 中文字体和 touch driver已观察；MIC、mode 与 provisioning触摸流程仍需物理交互闭环 |
| Wi-Fi/NVS/bootstrap | `partial` | 已观察 primary失败后 fallback、DHCP和公网 bootstrap；新增断线状态清理及 supervisor 重载完整 Wi-Fi plan，Wi-Fi flap与 NVS 保存重启回读仍需实机 |
| WSS voice loop | current artifact `not_run` | 至少完成两条真实回复，验证 session sequence 跨 response 单调，并覆盖 ASR final、LLM、完整 TTS、explicit cancel 与断线恢复 |
| UDP voice loop | baseline `passed` / current artifact `partial` | GCM probe、双向媒体、连续两轮和到期 fresh bootstrap已通过；当前 MIC-owned artifact仍需模式选择、loss/jitter/PLC 与显式 cancel |
| AEC/acoustic | `not_run` | 近讲、远讲、double-talk、自回授与 interrupt tail 评测通过 |
| Wake word | diagnostic artifact `device_verified` / release candidate `not_run` | `wn9s_hiesp` 已在独立 idle owner 实机启动并命中，feed/fetch task 有界退出后完成音频 owner handoff；current candidate 仍需重复唤醒/退出和异常停止回归 |
| Stability | 12-minute baseline `passed` / watchdog fix `device_observed` / release gate `not ready` | 修复 `pdMS_TO_TICKS(5)==0` 造成 playback busy loop 后连续约146秒无 task watchdog、断连或持续队列增长；正式发布仍须在最终可复现 artifact 完成20轮和30分钟 |
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
