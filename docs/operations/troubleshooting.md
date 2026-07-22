# 故障排查

更新日期：2026-07-22

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

若设备 bootstrap 得到 200、但 WSS/runtime 本地启动失败后立即重试出现 `route_already_leased`，检查设备是否已在
lease identity 验证后保留 worker/epoch/fencing、是否执行有界 `/v1/session/release`，以及日志中是否出现
`route release acknowledged`。重复/stale release 返回成功是预期幂等语义，不能据此推断当前 lease 被删除。

Worker 配置 Director 时，`/health/ready` 的 `coordination_ready=false` 表示 heartbeat 尚未成功；先修复
Director/Redis/internal token/网络，不要只检查 provider。Drain 是单向操作，误 drain 后应停止旧 Worker 并启动
replacement，不能向 v1 drain API 发送 `false`。

## 4. 401 / WSS 1008

- Bootstrap credential、worker-bound grant 和 lab token 不混用。
- `Device-Id` 与 grant `device_id` 一致，Worker ID 与 grant audience 一致。
- 系统时钟没有导致 `iat/exp` 错误。
- Grant 未重复使用；Worker 通过 Director 在 shared coordination store 原子消费 `jti`，重复使用应被拒绝。
- `Protocol-Version: 1` 存在。

不得把完整 bearer 放入日志或命令历史。

## 5. Hello/控制失败

- Client 首条应用消息是 hello。
- `audio_params` 为 Opus/16 kHz/mono/60 ms。
- `transport_profiles` 唯一且与 grant allowed profiles 相交。
- JSON 无 duplicate/unknown field，frame 未超过 hard limit。
- 后续消息 `session_id` 匹配。

Close `1002` 看 protocol category，`1009` 看消息大小，`1013` 看 admission/queue。

## 6. WSS 有连接但无 ASR

按顺序检查：

1. `session.opened` 已完成且设备处于 listening。
2. Worker收到 binary Opus，packet size/cadence/decode 无错误。
3. 解码 PCM 的 peak/RMS 非零；诊断仅显式短期开启。
4. Agent runner 为 `livekit`，FunASR URL/protocol/timeout 与远端一致。
5. ASR queue 未满，provider 没有 timeout/429/invalid response。
6. 设备采集/AEC/VAD 没有把语音全部门掉。

用固定 Opus/PCM host fixture 区分 transport 与麦克风，不能仅凭 UI“聆听”判定上行正常。

若 host fixture 可触发 ASR，而设备持续上传却没有 ASR，优先检查输入幅度、测试声源耦合、AFE/VAD 门限和
Opus 解码 PCM peak/RMS，再检查 grant、transport admission 与 provider endpoint。

## 7. UDP 无音频

- Server hello确实 commit `udp-opus-gcm-v1` 并包含 grant。
- UDP advertise host/port 从设备网络可达，防火墙/NAT 放行。
- PROBE 在 timeout 内到达，GCM auth 成功后才绑定 source并返回 PROBE_ACK。
- `media_id/epoch/direction key/salt/nonce/AAD` 与 fixture 规则一致。
- Replay、wrong source、queue dropped、lost/reordered/expiry 指标属于哪一类。
- WSS 是否仍连接；WSS 断开会撤销 UDP。

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

每次只调整一个 frame/buffer 参数并保留 A/B 音频与 timeline。

## 9. 自问自答 / 无法打断

- Board config 的 reference channel 和 device AEC 只是前提，需确认实际 AFE `MR`、reference 非零、播放期间
  realtime capture。
- 检查 speaker volume、麦克风距离、VAD threshold、播放 reference 时序和 generation。
- Abort 后确认 generation递增、旧服务队列清空、设备拒绝旧播放。
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

## 12. 收尾记录

故障记录至少包含环境、commit、artifact、redacted config shape、复现步骤、first failure、相关 metrics/log window、
修复前后验证和仍为 `not_run` 的项目。

当前分层对照基于 Server lifecycle repair `d2fa0ca`：launcher stop/start、bootstrap + grant、WSS hello/profile、
UDP AES-GCM probe ACK/ready，以及 host synthetic real-provider media E2E 均通过。若 ESP32 无 ASR，应优先比较
host synthetic 与设备解码 PCM，而不是重复修改已通过的 grant/probe 路径。Launcher stop 只终止 PID、启动时间
与 executable identity 都匹配的记录进程；不匹配条目会保留并告警，禁止按复用 PID 误杀其他进程。
