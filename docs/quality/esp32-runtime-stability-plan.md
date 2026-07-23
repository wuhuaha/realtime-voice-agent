# ESP32 端侧稳定性优化方案与实施计划

状态：active
更新日期：2026-07-23
依据：用户授权的端侧稳定性整改目标、`docs/decisions/0005-native-esp-idf-endpoint-and-rva-protocol.md`、当前
Firmware 架构和 RVA 协议边界。

## 1. 目标与非目标

目标：

- 保留 `UDP` 为 ESP32-S3 端侧首选媒体 profile，保留 `WSS` 为降级和对照 profile。
- 让 native ESP-IDF 固件在低内存、网络抖动、服务重启、用户打断和多次 start/stop 下保持确定行为。
- 将端侧问题从“反复手动点击观察症状”收敛为可自动采集的资源、状态、媒体和服务侧 timeline 证据。
- 修复当前已知的资源释放、部分初始化、热路径动态分配、队列溢出和错误恢复风险。
- 保持 Server、Firmware 和协议分层清晰；音频核心不依赖 UI，媒体热路径不访问 coordination store。

非目标：

- 不恢复或继续 Direct WebRTC、AIMP、PCM DataChannel 或研究仓归档实现。
- 不替换当前 `rva-control-v1`、`wss-opus-v2`、`udp-opus-gcm-v1` wire identity。
- 不引入新的 provider、模型 SDK、OTA、外部 broker 或持久化 coordination 依赖。
- 不把构建通过写成真机、声学或公网通过。

## 2. 问题判断

当前不稳定不代表 UDP/WSS 协议不可行。已验证历史路径说明板卡、Wi-Fi、I2S、屏幕和基础语音链路可以工作；
当前 native 路线的问题集中在实现层。

已知高风险信号：

- 端侧曾在启动 UDP/media task 前后出现 internal RAM 剩余极低，甚至接近数百字节。
- 曾出现 `xQueueGenericSend -> pthread_mutex_unlock -> std::mutex::unlock -> UdpSession::Revoke()` 路径崩溃，
  指向 teardown、mutex 和低内存组合风险。
- `xQueueCreateWithCaps()` / `xEventGroupCreateWithCaps()` 已被使用，但仍存在未配对的普通 delete API。
- WSS callback 和 frame assembler 存在热路径动态分配、`std::vector` 增长和 front erase。
- 队列满、UDP probe 失败或 grant/fallback 异常时，端云日志不足以一次性定位到具体层。

## 3. 目标端侧运行模型

```text
Idle
  -> ControlConnecting
  -> SessionOpening
  -> MediaProbing
  -> MediaRunning
  -> Stopping
  -> Backoff
```

状态语义：

- `ControlConnecting` 只启动 WSS control owner 和 supervisor，不启动 capture、AFE、codec 或 playback。
- `SessionOpening` 只发送 `session.open` 并等待 `session.opened`。
- `MediaProbing` 对 UDP 执行 socket open、session grant、probe/ack；成功后才启动 AudioCore。
- UDP probe 失败时优雅降级 WSS；降级必须释放当前 UDP media session，不复用无效 UDP key/source。
- `MediaRunning` 后才启动 capture/uplink/playback task。
- `Stopping` 只由 supervisor 执行，callback、UDP rx task、audio task 不销毁 owner 级资源。
- `Backoff` 用 bounded retry，不把普通连接失败转成设备重启。

## 4. 资源与并发规则

| 资源 | Owner | 内存策略 | 满载/失败语义 |
| --- | --- | --- | --- |
| WSS control owner | supervisor | control metadata 优先 internal；payload 固定 ring 可用 PSRAM | callback 不阻塞、不 close，只入队或计数丢弃 |
| UDP session/key/source | UDP supervisor | keyed context 与小状态优先 internal | auth fail 不推进 sequence；source 固定；revoke 只由 supervisor 触发 |
| UDP rx task | UDP runtime | task stack 可按实测放 PSRAM；socket/lwIP 状态不假设可控 | 只接收入队与 jitter，不做复杂 teardown |
| playback queue | media runtime | 非 DMA 大 payload 可放 PSRAM；caps create/delete 配对 | 满时丢旧或丢新并计数，不默认杀会话 |
| capture/uplink/playback task | AudioCore | DMA/I2S buffer 使用外设要求 capability；大 scratch 预分配 | 部分启动失败必须逆序清理并有界 join |
| UI queue/font | UI owner | font/文本优先 flash/PSRAM | UI 丢事件不影响音频 |

通用规则：

- 热路径不做无界日志、JSON 解析、Flash 写入或外部重连。
- callback 和音频循环不进行 `new`/`delete`、`std::vector` 增长、front erase。
- `std::mutex` 不放在 UDP/WSS/audio teardown 热路径；优先使用单 owner queue、fixed ring 或 ESP-IDF/FreeRTOS
  明确 capability 的同步原语。
- 所有 `WithCaps` 创建的 FreeRTOS 对象必须用对应 `WithCaps` delete。
- 所有 queue/event/task 有明确 producer、consumer、容量、停止条件、重复 stop 语义。

## 5. 音频实时性策略

默认 codec profile：

```text
16 kHz mono
Opus 60 ms
complexity 0
DTX enabled
FEC disabled initially
```

端侧播放策略：

- UDP 下行通过 jitter/freshness/generation 后才进入 Opus decoder。
- 缺包到 deadline 后使用 Opus PLC；迟到超过 freshness gate 的 TTS 直接丢弃。
- 打断时先推进 generation fence、清空 playout，再通知服务侧。
- 解码后按 10 ms 小块写 playback，保证打断响应；`WritePlayback()` 超时计数并分类。

端侧采集策略：

- AEC/VAD 保持可配置，默认沿当前已验证板级音频配置启用。
- VAD 主要用于 barge-in；ASR 分段和语义结果仍由服务侧负责。
- 如果 AEC/VAD 造成资源风险，必须以 HIL 指标证明后再降级，不能凭主观听感关闭。

## 6. 服务侧配合优化

服务侧需要提供可定位证据，避免端侧每次只能靠 UI 状态猜测。

关键 timeline 事件：

- `bootstrap.created`
- `grant.consumed`
- `ws.accepted`
- `session.open.received`
- `session.opened.sent`
- `udp.grant.created`
- `udp.probe.received`
- `udp.probe.ack.sent`
- `media.ready` 或 `media.fallback`
- `first.uplink.audio`
- `first.asr.partial`
- `first.asr.final`
- `first.tts.packet`
- `response.begin`
- `response.end`
- `close.reason`

错误语义：

- grant reject 必须区分 invalid、expired、wrong worker、wrong device、route mismatch、replay，但日志不得输出 token。
- UDP probe timeout 必须有独立 close reason 或 session event。
- input/output queue full 是 backpressure/QoS 事件；能丢旧实时媒体时不直接关闭整个会话。

## 7. 实施批次

### 批次 A：确定性资源与生命周期修复

- 修正 `xQueueCreateWithCaps` / `xEventGroupCreateWithCaps` 的 delete API 配对。
- `xQueueReset(playback_queue_)` 全部判空。
- `StartMediaRuntime()` 在 `codec/pipeline/resampler` 启动失败且还未创建媒体任务时逆序释放资源；一旦
  capture/uplink/playback 任一媒体 task 已启动，后续部分启动失败采用 fail-closed restart。原因是实机已观察到
  媒体 task 可能已经进入 ESP-SR、Opus、websocket 或 I2S 路径，此时原地 teardown 容易触发 UAF/heap/VFS 崩溃。
- `UdpRuntime` event group 使用 caps API；析构路径不把普通 bounded close 超时转为默认 abort。
- 增加关键状态和资源日志：internal free、largest block、PSRAM free、task high-water、queue drop、UDP probe。

验证：

- host 编译或 focused test。
- `rg` 检查 caps pairing 和 null queue reset。
- ESP-IDF clean build。

### 批次 B：WSS 热路径固定化

- `CallbackEventQueue` 改为固定容量 ring。
- callback 中不分配 payload，不移动 `std::vector`。
- `FrameAssembler` 改为 fixed buffer 或等效 bounded buffer。
- 测试 oversize、fragment、queue full、dropped counter。

验证：

- `firmware/components/transport_wss/host_tests/run.ps1` 或等价 host test。
- ESP-IDF build。

### 批次 C：服务侧可观测与降级契约

- 增加 Director grant consume 和 Worker session 的结构化原因日志。
- UDP probe timeout、fallback、close reason 可区分。
- 测试 reject reason 不泄密，ready wait 和 run-local 保持稳定。

验证：

- `uv run --directory server pytest ... -q` focused tests。
- 不输出 token/key/raw audio/provider 原始 body。

### 批次 D：HIL 一键门禁

- 脚本自动启动服务、抓串口、等待端侧状态、解析 session timeline。
- 覆盖 WSS、UDP、UDP fallback、服务重启、Wi-Fi 重连、连续 start/stop。
- 生成单次 summary，而不是要求人工反复点击。

验证：

- 至少一次 `boot_observed`。
- 至少一次 `device_verified` 的 UDP real-provider 闭环。
- 至少一次 WSS fallback 闭环。

## 8. 验收标准

候选阈值，首次实测后冻结或调整：

- `MediaRunning` 后 internal free 不低于 32 KiB，largest internal block 不低于 16 KiB。
- 连续 50 次 start/stop 后 internal heap 漂移小于 5 KiB。
- 音频 task stack high-water 保留至少 2 KiB。
- 单设备 30 分钟 soak 无 Guru、WDT、非预期 reboot。
- 稳定网络下 uplink Opus packet cadence 为 60 ms ± 15 ms。
- 播放队列最大媒体年龄低于 240 ms；过期媒体被丢弃，不追播旧 TTS。
- 打断尾音候选目标小于 200 ms。
- UDP probe 失败时 2 秒内切到 WSS 或进入可恢复 backoff。

未完成 HIL 前，任何结果只能标为 `build_passed`、`host_verified` 或 `boot_observed`，不能声称稳定通过。

## 9. 回滚与停止条件

回滚点：

- 批次 A 修复后应仍能保持 WSS baseline。
- 批次 B 若破坏 WSS contract，应只回滚 WSS queue/assembler，不回滚端侧生命周期修复。
- 批次 C 若影响 grant 单次消费安全，应回滚服务侧日志/契约改动。

停止条件：

- ESP-IDF build 无法恢复。
- 端侧 internal RAM 无法达到最低运行阈值，且关闭非必要 UI/NS/日志后仍不足。
- UDP/WSS 均无法通过 reference client 或真机基础闭环。
- 出现 secret/raw audio 泄漏风险。

## 10. 当前进度日志

- 2026-07-23：建立本计划；开始并行实施批次 A、B、C。当前禁止把未完成 HIL 的结果写成稳定通过。
- 2026-07-23：批次 A 已完成第一轮代码收敛：修正 caps FreeRTOS 对象释放配对、补齐 playback queue 判空、
  media partial-start cleanup 和 UDP join timeout fail-closed。状态：`build_passed`，尚未烧录和 HIL。
- 2026-07-23：批次 B 已完成第一轮 WSS callback queue 固定 ring：callback 不再为 payload 执行 `new`，不再
  `vector.erase(begin)`；FrameAssembler 的完整 fixed-buffer 化仍保留为后续优化。状态：`host_verified` +
  `build_passed`。
- 2026-07-23：批次 C 已完成第一轮服务侧可观测性：grant reject reason、WebSocket accept/reject、RVA/Xiaozhi
  session open/close、UDP probe timeout 均有可区分日志/close reason，日志不输出 token/key/raw audio。状态：
  `unit_verified`。
- 2026-07-23：端侧临时改为开机自动建链，MIC 按钮不再控制 session lifecycle。HIL 证据显示：
  `auto_start=1` 后设备可自动 bootstrap、WSS control accepted、选择 `udp-opus-gcm-v1`，并完成 UDP probe。
  首轮自动建链暴露两个端侧实现问题：媒体任务使用 internal stack 导致 playback task 创建失败；部分媒体任务启动
  后原地清理 audio/websocket 资源会触发 UAF/heap/VFS 崩溃。已将 capture/uplink/playback task 改为
  caps-aware PSRAM stack，并将媒体 partial-start failure 改为 fail-closed restart。状态：`build_passed` +
  `device_observed`。
- 2026-07-23：WebSocket 运行期 panic 解码到 ESP-IDF `esp_websocket_client_task -> select() ->
  uart_vfs.c:uart_end_select()`，触发条件与 UART console VFS 参与 lwIP/websocket select 相关。已将应用 console
  从 UART0 切到 USB Serial/JTAG，避免 UART VFS 进入 websocket select 路径。注意：当前 CH340/COM14 打开会导致
  开发板复位，因此常规稳定性观察不应反复打开串口；服务侧 session health 与结构化日志作为主证据。
- 2026-07-23：重启服务后端侧自动重连，服务侧 `active_sessions=1`。新增 `rva_media_input` 低频日志确认：
  UDP 上行包进入 Worker，Opus 解码后推入 LiveKit Agent。真实链路观察到 `user_speech_started`、
  `asr_request_started`、`asr_provider_final`、`asr_final`、`llm_requested`、`tts_requested`、`tts_first_pcm`、
  `endpoint_playback_started` 和 `agent_audio_published`。一次 60 秒服务侧稳定观察保持 `active_sessions=1`，无新增
  `session_closed`。状态：`device_verified_basic_chain`；仍未完成长稳、真实听感和多轮打断验收。
