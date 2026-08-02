# 决策 0006：服务端独占语义打断并以 RVA Protocol 1.0 收敛端云协议

日期：2026-07-23
状态：accepted

## 背景

Native endpoint 已使用项目自有协议，但旧 control contract 仍允许端侧 VAD 在服务端裁决前清空播放并发送 `barge_in`。
Worker 同时会在播放期间抑制上行音频，LiveKit 默认也会把不可打断阶段的 STT 输入替换成静音，导致服务端明确
短语策略在真实链路中不可达。一个 Agent speech 的多个 TTS flush 还可能被映射成多个 playback generation。

本项目不再以旧固件兼容或 dual-stack 过渡为目标，需要把 semantic decision、response lifecycle 和物理播放事实
放回各自唯一 owner。

## 决定

- `InterruptionCoordinator` 是语音打断接受的唯一权威；首版策略只接受版本化的明确命令语法。
- Roomless LiveKit `AgentSession` 保留 VAD、STT、EOU、turn、LLM/TTS 和 public force interrupt，不按普通端侧
  VAD activity 自动终止回答。
- `ResponseCoordinator` 独占 response id、downlink generation、terminal outcome 和 output fence。一次 Agent speech
  只有一个 response/generation，多次 TTS flush 只是同一 response 的媒体片段。
- Endpoint 不从 VAD、wake onset 或普通 speech 推导 cancel；AEC、NS 和 VAD 继续服务音频质量、UI 和观测。
- Endpoint 只执行服务端精确 `playback.stop`、generation fence 和 sequence drain，并回报物理
  `playback.started/ended`。明确按钮操作可以立即关闭本地 render gate并请求取消。
- Current wire提升为 `rva/1`、`/rva/v1/voice`、`wss-opus/1` 和 `udp-opus-gcm/1`。不运行旧 wire
  dual stack，也不保留 legacy firmware route。
- 两个 v1 media profile 的 uplink generation固定为0；downlink generation只约束 render freshness。
- 正常 response terminal携带最后媒体序号，设备播放到该序号后才报告 completed；取消和失败不冒充正常完成。

## 实现约束

- 播放期间 AEC 后音频必须真实到达同一 FunASR stream。LiveKit automatic interruption关闭时，显式设置
  `discard_audio_if_uninterruptible=false`；端侧已提供 AEC 的 binding不使用 Room AEC warmup丢音。
- strict policy不使用任意子串命中。Acoustic/echo evidence可以否决候选，但不能把非白名单文本升级为 interrupt。
- cancel先在短临界区推进 fence并通知设备，再在临界区外 bounded interrupt Agent/provider；迟到 callback按 token
  丢弃。
- capture/uplink不得直接 reset playback queue。Playback task独占 decoder、jitter/PCM queue与DAC，并在 I2S
  dequeue前执行最终 generation gate。
- correctness baseline使用连续 Opus cadence + DTX。VAD-gated uplink只有在 pre-roll、hangover、EOU和资源实验
  通过后才能作为新的协商 profile。

## 已考虑选项

- v1兼容 shim：不能恢复端侧已经截断的音频，拒绝。
- 端侧提高 VAD 阈值后继续裁决：仍存在双权威和回声误判，拒绝。
- LiveKit native adaptive/VAD interruption直接裁决：不能表达 strict explicit policy，暂不采用。
- 正常 turn与打断分别运行 ASR：资源与一致性成本更高，仅在单 stream public API不满足时复查。

## 后果与部署

Server 与 Firmware 必须作为匹配 artifact 部署；当前 endpoint 只连接注册表中的 `rva/1` Worker。任一组件的
protocol/profile identity 不匹配时必须 fail closed，并通过 fresh bootstrap/reconnect 恢复。浏览器与移动端继续
使用标准 LiveKit Room，不使用 ESP32 RVA profile。

## 复查触发条件

- pinned LiveKit public API无法在一个 STT stream内隔离 ignored overlap、accepted interrupt和新 turn。
- 连续 DTX在目标板上造成 watchdog、deadline或不可接受的资源余量。
- 新 endpoint需要已验证且无法由 capability/profile extension表达的不同 playback fact或cursor语义。

## 关联

- [决策 0005：Native ESP-IDF endpoint](0005-native-esp-idf-endpoint-and-rva-protocol.md)
- [Server 架构](../architecture/server.md)
- [Firmware 架构](../architecture/firmware.md)
- [RVA Protocol 1.0](../protocol/rva-protocol-v1.md)
