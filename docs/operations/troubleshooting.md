# 故障排查

更新日期：2026-07-30

## 1. 排查原则

先定位层级，再改变配置。每次只变一个变量，并记录 `device_id/worker_id/session_id/epoch/profile/generation`。
不得通过打印 token、key、Wi-Fi 密码、原始音频或 provider body 获取便利。

```text
repository/config -> Director/Redis -> Worker WSS/UDP -> codec/audio -> Agent/provider -> firmware/UI/acoustic
```

## 2. 基础检查

```powershell
./scripts/verify.ps1
git status --short
Invoke-RestMethod "$env:VOICE_DIRECTOR_URL/health/ready"
Invoke-RestMethod "$env:VOICE_WORKER_URL/health/ready"
```

确认 `.env` 和 `.env.local` ignored，endpoint 可达，时钟同步，Worker public URL/UDP advertise 是设备可访问地址，
不能误用仅在 Worker 本机可达的 loopback 地址。

## 3. Director 找不到 Worker / bootstrap 503

检查：

- Worker heartbeat enabled、Director URL 和 `VOICE_INTERNAL_TOKEN` 一致。
- Worker heartbeat 未过期，`healthy=true`、`draining=false`、`active < max`。
- requested profiles 与 Worker profiles 有交集。
- Redis ready；生产没有误用 memory backend。
- Redis connect/command timeout 未被设为不合理的大值；timeout 应返回脱敏
  `503 {"detail":"coordination_unavailable"}`，不能挂住 bootstrap 或泄露 Redis URL。

`no_capacity` 与“服务宕机”不同。不要盲目提高 `max_sessions`，先看 active session、CPU/RSS/event-loop/provider
pressure 和未释放 session。

设备 bootstrap 与 Worker 重启窗口重合时，可能连续收到短时 503 或单次请求 timeout；先关联 Worker incarnation、
heartbeat 和 readiness 时间线。若 Worker ready 后设备通过有界退避自动恢复并成功 `session.opened`，这是启动窗口的
可恢复失败，不应归因为 Wi-Fi/NVS。若 ready 后仍持续 503，再检查 stale drain、旧 `EnvironmentFile` identity、route
lease 和 capacity；不要通过无界快速重试制造额外压力。

若设备 bootstrap 得到 200、但 WSS/runtime 本地启动失败后立即重试出现 `route_already_leased`，检查设备是否已在
lease identity 验证后保留 worker/epoch/fencing、是否执行有界 `/v1/session/release`，以及日志中是否出现
`route release acknowledged`。重复/stale release 返回成功是预期幂等语义，不能据此推断当前 lease 被删除。

Worker 配置 Director 时，`/health/ready` 的 `coordination_ready=false` 表示 heartbeat 尚未成功；先修复
Director/Redis/internal token/网络，不要只检查 provider。Drain 是单向操作，误 drain 后应停止旧 Worker 并启动
replacement，不能向 v1 drain API 发送 `false`。

若 systemd unit 中的 `Environment=` 已更新，但 Worker 仍以旧 identity 启动，检查实际加载顺序。后加载的
`EnvironmentFile` 会覆盖 unit 内同名变量；应更新受控 runtime env 中的 `VOICE_WORKER_ID`，再重启并以 readiness、
heartbeat 和进程日志三方核对唯一 incarnation。不要只修改 unit 后反复重启，也不要复用已进入 sticky drain 的
旧 Worker identity。

## 4. 401 / WSS 1008

- Bootstrap credential、worker-bound grant 和 lab token 不混用。
- `Device-Id` 与 grant `device_id` 一致，Worker ID 与 grant audience 一致。
- 系统时钟没有导致 `iat/exp` 错误。
- Grant 未重复使用；Worker 通过 Director 在 shared coordination store 原子消费 `jti`，重复使用应被拒绝。
- `Authorization: Bearer <grant>` 存在，且 `Device-Id`（或兼容入口的 `Client-Id`）与 grant 绑定的设备一致。

不得把完整 bearer 放入日志或命令历史。

## 5. `session.open` / 控制失败

- Client 首条且唯一的 open 消息是 `session.open(protocol_version=1)`；不得发送未注册消息。
- `audio` 为 Opus/16 kHz/mono/60 ms，DTX on、FEC off。
- `supported_media_profiles` 只包含 `wss-opus/1`/`udp-opus-gcm/1`，且与 grant allowed profiles 有交集；
  `preferred_media_profile` 必须属于该集合。
- JSON 无 duplicate/unknown field，frame 未超过 hard limit。
- 后续消息的 `session_id + session_epoch` 必须匹配 `session.opened` 建立的 identity。

Close `1002` 看 protocol category，`1009` 看消息大小，`1013` 看 admission、queue 与 uplink freshness/backpressure；
`1011/runtime_failure` 表示 unexpected AgentSession/provider terminal。两者均要求 fresh bootstrap，不表示同 session
已恢复。

## 6. WSS 有连接但无 ASR

按顺序检查：

1. `session.opened` 已完成且设备处于 listening。
2. Worker收到 binary Opus，packet size/cadence/decode 无错误。
3. 解码 PCM 的 peak/RMS 非零；诊断仅显式短期开启。
4. Agent runner 为 `livekit`，FunASR URL/protocol/timeout 与远端一致。
5. ASR queue 未满，provider 没有 timeout/429/invalid response。
6. 设备采集/AEC/VAD 没有把语音全部门掉。

用固定 Opus/PCM host fixture 区分 transport 与麦克风，不能仅凭 UI“聆听”判定上行正常。

`provider_network=ready` 或 `/health/ready` 只说明 endpoint、配置与探测请求可达，不证明一次真实推理成功。正式
provider canary 必须使用与 runtime 相同的 stream/final wire contract，并分别确认 ASR final、LLM 输出和 TTS PCM。
若 readiness 通过但 canary 返回 `invalid_audio`、`unknown` 或缺少 final，先对照 provider 的实际响应字段、终止消息和
音频格式；不得把网络 readiness 写成 ASR verified，也不得把未知协议错误宽泛降级成 no-speech。

若 host fixture 可触发 ASR，而设备持续上传却没有 ASR，优先检查输入幅度、测试声源耦合、AFE/VAD 门限和
Opus 解码 PCM peak/RMS，再检查 grant、transport admission 与 provider endpoint。

Standalone FunASR 返回 `no_speech`、空 `final`，或精确的旧协议
`invalid_audio + "FunASR returned empty text"` 时，应看到 `asr_no_result`，当前 turn 无文本结束，session 不应因此
关闭。其他 `invalid_audio` 属于实际音频/协议错误，不得宽泛降级为空结果；`busy`、`inference_failed` 才是当前定义的
retryable provider error。若先看到 LiveKit `session_closed`，再看到 RVA input queue 逐渐填满，说明 Agent terminal
没有及时上抛或运行 artifact 过旧；正确行为是立刻撤销 input admission，best-effort 发 `session.error`，并以
`1011/runtime_failure` fresh reopen。

## 6.1 已有 ASR final，但没有 TTS

先按事件顺序定位，不要先改 UDP 或端侧播放：

1. 确认同一 `turn_id` 出现 `asr_final` 和 `llm_requested`。
2. 若没有 `tts_requested`，检查 LLM streaming 的错误、超时和首 token；DeepSeek-compatible provider 应使用
   `VOICE_LLM_READ_TIMEOUT_SECONDS`，不要依赖 SDK 的默认 read deadline。
3. 若已有 `tts_requested` 但没有 `tts_first_pcm`，检查 TTS provider、流式响应和 provider timeout。
4. 若已有 `tts_first_pcm` 但没有 `endpoint_playback_started`，检查 Agent playback/generation；若已开始播放但
   session close 统计的 `downlink_packets=0`，再检查 Worker media binding。
5. `downlink_packets>0` 时，才继续检查 UDP/WSS transport、jitter、Opus decode 和端侧 playback queue。

LLM 失败时没有 TTS 下行是正常因果链，不应误诊为端侧没有播放。日志只能关联脱敏的 `turn_id`、`session`、
`worker_id` 和计数器，不得加入 prompt、token 或 provider 原始响应。

## 7. UDP 无音频

- `session.opened` 确实 commit `udp-opus-gcm/1` 并包含 grant。
- UDP advertise host/port 从设备网络可达，防火墙/NAT 放行。
- PROBE 在 timeout 内到达，GCM auth 成功后才绑定 source并返回 PROBE_ACK。
- `media_id/epoch/direction key/salt/nonce/AAD` 与 fixture 规则一致。
- Replay、wrong source、queue dropped、lost/reordered/expiry 指标属于哪一类。
- WSS 是否仍连接；WSS 断开会撤销 UDP。
- 固件实际 build config 是否允许并选择 UDP。UI 显示、NVS preference 或源码默认值不等于最终编译配置；若
  `session.opened` 始终为 `wss-opus/1`，先检查 default profile 的 Kconfig/SDKCONFIG 是否被硬编码为 WSS，再分析
  PROBE 或 GCM。修复后必须以新 artifact identity 重跑，不能沿用旧固件的 UDP 结论。

若设备 `sendto` 连续成功但 PROBE timeout，必须在 Worker advertise endpoint 对应主机抓取目标 UDP 端口：

- 主机能看到 datagram：继续检查 GCM admission、`media_id/epoch`、source binding 和 PROBE_ACK 回程。
- 主机看不到 datagram：故障边界位于 Worker 进程之前，检查公网 UDP ingress、云防火墙、安全组、NAT 和 advertise
  映射；`sendto` 成功只表示数据已交给本地网络栈，不表示公网服务端收到。
- 失败后设备按协议 fresh bootstrap 并安全回退 WSS，可以维持可用性，但不能把 fallback 记作 UDP 通过。

一个历史网络窗口（artifact identity未完整登记，不能作为当前门禁）曾出现 cold default probe 3/3 timeout、设备
12 次 send 成功而 Worker 主机 90 秒目标端口抓包为 0；后续 `ae56fac` 在另一已登记窗口完成 authenticated probe、
双向媒体和 normal close。前一现象只保留为“公网 ingress 在进程前丢包”的诊断样例，不表示当前 UDP cold entry
仍失败；复现时必须绑定 source、网络和抓包证据，不通过随机切换端口或放宽 GCM/probe deadline 猜测性修复。

不要在同 session 手工切回 WSS。关闭后 fresh bootstrap，必要时用 `force_wss` 建立对照。

## 8. TTS 每段内部卡顿

区分 provider PCM discontinuity 与端侧 underrun：

- 保存经过授权的短时 downlink PCM/Opus诊断 artifact，检查 20/60 ms cadence、sample rate、零填充和断点。
- 查看 TTS first PCM、chunk gap、Opus send cadence、network queue、device jitter/underrun。
- 确认 callback carry buffer 不丢弃非整帧尾部。
- 确认 WSS TCP HOL 或 UDP reorder deadline没有使 media age增长。
- 确认播放 queue 既不为零也不通过大预缓冲掩盖延迟。
- UDP 设备侧 `media_age_dropped` 增长表示 frame 在最终 360 ms gate 被拒绝；先查调度阻塞、reorder 和播放 queue，
  不要简单放宽 gate 来掩盖过时 TTS。
- Server `overload_source=opus_input_stale` 表示当前只观察到一个 stale packet 且 queue 未满，不要求一定存在 fresh
  packet；`opus_input_backpressure` 表示连续 stale 或真实 queue pressure。若同时出现 `qsize=0`、`dropped_packets=1`
  和 `fresh_packet_available=false`，不得仅凭 `1013` 将其诊断为队列阻塞。检查端侧 pre-send drop/local send timeout、
  packet timestamp cadence、TCP stall/burst 与 Server media-timeline age。
- WSS 日志 `rva_uplink_stale_recovered` 表示旧包在 Opus decode 和 runner push 前被丢弃，同一 session 继续；10 秒内
  最多恢复 2 次。若没有 fresh packet，随后应看到 `rva_uplink_catchup_completed`：Server 丢弃 TCP 缓存追赶包，
  连续观察到 2 个接近 60 ms 的到达间隔后才恢复 decoder 输入。关闭统计的 `recovered_catchup_packets` 只累计已经
  完成的 catch-up 丢包；`rva_uplink_catchup_exhausted` 表示两倍 freshness budget 内未抵达 live edge。
- `rva_uplink_stale_recovery_exhausted`、`rva_uplink_catchup_exhausted`、partial runner push、UDP stale 和真实
  backpressure 均以 `1013/media_overloaded` fresh reopen，而不是 `1002/protocol_error`。该恢复不是
  `AgentSession` reset；出现恢复后应确认同一 session 继续收到 ASR/TTS，而不是只看 WebSocket 未断开。不要扩大
  队列保存旧上行。

每次只调整一个 frame/buffer 参数并保留 A/B 音频与 timeline。

## 9. 自问自答 / 无法打断

- Board config 的 reference channel 和 device AEC 只是前提，需确认实际 AFE `MR`、reference 非零、播放期间
  realtime capture。
- 检查 speaker volume、麦克风距离、VAD threshold、播放 reference 时序和 generation。
- `playback.stop` 后确认服务端 fence generation 单调推进、旧服务队列清空、设备拒绝旧播放；明确用户操作应发送
  exact-target `response.cancel.request`，端侧 VAD 不得自行裁决打断。
- 用近讲/double-talk固定话术，不能只在静音房间观察。

最终 AEC/声学当前为 `not_run`，旧 baseline 不能替代。

若日志提示 ESP-SR `model` 分区不可用，当前预期是仅关闭神经网络降噪，AEC/VAD 仍初始化；这不证明实际声学效果。
分别记录 `frontend.aec_enabled/vad_enabled`、近讲/播放中 double-talk、上行 PCM 与 ASR，对照补烧 model 分区后的结果。

## 10. Worker 停止卡住 / route 长时间不释放

- 确认 shutdown 日志先发布 `draining=true`，再关闭 registry，最后上报 pending exact releases。
- 默认总预算 10 秒、最多 32 次 release heartbeat；达到任一上限会带 pending 数量告警后退出，这是有界退化而非
  无限重试。
- 若 TTS 并发槽已满，默认等待 0.25 秒后产生 retryable backpressure；不要通过无界调大 queue timeout 延长关停。
- MiMo 返回异常大 SSE 时应在单行/事件 1 MiB、256 data line、512 KiB decoded chunk 或 8 MiB total response 边界
  失败；若内存持续增长，核对运行 artifact 是否包含这些 parser gates。

## 11. 固件黑屏/启动失败

- 确认烧录的是 `firmware/apps/voice_terminal` 的完整 native artifact；`firmware/device` 仅是 headless contract
  harness，不包含板级、显示或音频运行时。
- 核对 artifact SHA-256、target、partition、ESP-IDF、board 和完整 boot log。
- 先看 reset reason、panic/WDT、PSRAM、LCD init、backlight 与 LVGL task，再看网络。
- 不用擦 NVS 掩盖启动 bug；只有明确验证配置迁移且已备份时才擦除。

从 fresh commit/worktree 构建设备 HIL artifact 时，不得隐式依赖未跟踪的 `sdkconfig.local`。先生成或注入本轮明确的
SDKCONFIG 配置，保存脱敏后的配置 identity，再执行 clean build；否则即使源码 commit 相同，也可能得到缺少 board、
Wi-Fi、bootstrap 或 feature 配置的不可用固件。配置化 variant 与纯默认 variant 必须分别命名，不能共享“同一固件”结论。

该板卡的完整 artifact 包含 bootloader、partition table、app、`srmodels` 和 font assets 五部分。烧录前按 build
产出的 flash args 核对地址与 hash；遗漏 model/font 分区可能表现为唤醒、降噪或中文显示异常，不应只重刷 app。
按 CMake 设计，`font-assets` 仅是 `flash`/`font-assets-flash` 的依赖：普通 `idf.py build` 不生成
`font_assets.bin`，这不属于 app build failure。使用 `idf.py flash` 时会自动生成；若先制作五分区 manifest 或手工
调用 esptool，必须先显式执行 `idf.py font-assets`，再对生成文件计算 hash 并烧录。
默认 HIL 保留 NVS，以验证现有配置迁移和重启回读；只有配置 schema 不兼容、NVS 损坏或测试明确要求 clean-state 时
才擦 NVS，并记录原因。擦除后必须使用配置化 artifact 或 provisioning 流程恢复配置，不能用擦 NVS 掩盖连接状态机问题。

## 12. 收尾记录

故障记录至少包含环境、commit、artifact、redacted config shape、复现步骤、first failure、相关 metrics/log window、
修复前后验证和仍为 `not_run` 的项目。

其他 source、artifact、commit 或 host synthetic 结果都不能作为当前闭环证据。若 ESP32 无 ASR，应使用同一
Product source identity 的 reference client 与设备解码 PCM 做分层对照，并分别记录 bootstrap、`session.opened`、
media admission、provider 和 playout 证据；未执行的层级保持 `not_run`。Launcher stop 只终止 PID、启动时间与
executable identity 都匹配的记录进程；不匹配条目会保留并告警，禁止按复用 PID 误杀其他进程。
