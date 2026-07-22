# Xiaozhi Control Protocol v1

状态：legacy compatibility only
Wire ID：`xiaozhi-control-v1`
Path：`/v1/xiaozhi`

## 1. WebSocket 建连

Client 请求头：

| Header | 要求 |
| --- | --- |
| `Protocol-Version` | 必须为 `1` |
| `Device-Id` | 物理设备标识；归一化后作为 principal |
| `Client-Id` | 兼容标识，可校验但不得覆盖有效 `Device-Id` |
| `Authorization` | `Bearer <connect-grant>`；开发模式可配置 lab token |

生产使用 `wss://`。Header 缺失、无效、过期、错 Worker/设备或已消费 grant 必须在创建 AgentSession 和
媒体资源前拒绝。

## 2. JSON 规则

- Text frame 必须是 UTF-8 JSON object。
- 重复 key、unknown field、错误类型、超限文本和错误 session fail closed。
- Core message 以 `protocol/xiaozhi_control_v1/messages.schema.json` 为准。
- v1 control frame 上限由 Worker hard config 约束，默认不超过 16 KiB。
- 不记录原始 payload；诊断只记录 message type、长度、session 和拒绝类别。

## 3. Hello

### Client -> Server

```json
{
  "type": "hello",
  "version": 1,
  "transport": "websocket",
  "audio_params": {
    "format": "opus",
    "sample_rate": 16000,
    "channels": 1,
    "frame_duration": 60
  },
  "transport_profiles": ["wss-opus-v1", "udp-opus-gcm-v1"],
  "transport_mode": "auto",
  "features": {}
}
```

Hello 必须是应用层首条消息。省略 `transport_profiles` 等价于只支持 WSS；省略 `transport_mode` 等价于
`auto`。

### Server -> Client

```json
{
  "type": "hello",
  "version": 1,
  "transport": "websocket",
  "transport_profile": "wss-opus-v1",
  "session_id": "opaque-session-id",
  "audio_params": {
    "format": "opus",
    "sample_rate": 24000,
    "channels": 1,
    "frame_duration": 60
  }
}
```

选择 UDP 时 `transport` 为 `udp`，并包含 schema 定义的 `udp` grant。Client 只有完成 grant 解码、
authenticated probe 和 `PROBE_ACK` 后才能发送 UDP AUDIO。

## 4. Listen

Client -> Server：

- `state=start`：开始一个 listening window；`mode` 可为 `auto/manual/realtime`。
- `state=stop`：停止上行语音输入并请求收敛当前识别。
- `state=detect`：兼容唤醒检测事件，可携带长度受限的 `text`。

`realtime` 用于 AEC 支持下的播放期间持续采集。重复 start/stop 必须有确定的幂等或协议错误行为，不能创建
第二个 AgentSession。

## 5. STT

Server -> Client：

```json
{
  "session_id": "opaque-session-id",
  "type": "stt",
  "text": "你好",
  "is_final": false
}
```

Partial 可覆盖设备显示但不得提交为长期对话事实；`is_final=true` 表示当前 utterance 的最终文本。文本上限
为 16 KiB。Server 不得发送其他 session 的转写。

## 6. TTS

Server -> Client state：

- `start`：当前 generation 开始响应。
- `sentence_start`：可携带本段字幕 `text`。
- `stop`：该 generation 正常结束或被 interruption fence 收敛。

`generation` 是 downlink freshness fence。Client 观察到更高 generation 必须清除低 generation 排队音频；
低于当前 generation 的 UDP AUDIO 必须丢弃。WSS binary frame没有 generation header，因此 Client 必须在
处理 `abort/TTS stop` 时同步清理本地 WebSocket 音频队列。

## 7. Abort

Client -> Server：

```json
{
  "session_id": "opaque-session-id",
  "type": "abort",
  "reason": "wake_word_detected"
}
```

Worker 必须串行化 interruption：推进 generation、停止接受旧输出、取消/中断 AgentSession response、清理
队列并发送收敛的 TTS state。重复 abort 不得恢复旧音频或重复副作用。

## 8. MCP 兼容扩展

现有 Xiaozhi endpoint 可能发送 `type=mcp`。首版 Worker 可将其作为 bounded opaque JSON 兼容处理，但它不在
core schema 内，不得绕过 frame size、duplicate key、session、鉴权和日志脱敏，也不得成为本产品功能验收的
隐含依赖。需要稳定 MCP 能力时必须独立版本化 schema。

## 9. 关闭

Core v1 不新增未定义的 `error` JSON。错误通过标准 WebSocket close code、有限 reason 和 server metrics 表达。
具体映射见 [生命周期与错误](lifecycle-errors.md)。

## 10. 验证边界

Canonical schema/fixtures 已通过 contract tests。Server `fca8de8` + repair `259aeee` 使用 Director grant 和
shared `jti` 原子消费完成 host synthetic real-provider media E2E。Final firmware 已完成唤醒、WSS handshake、
UDP authenticated probe 和持续 UDP 上行；设备输入未触发 ASR，因此 STT/TTS/abort 的真机端云行为仍为
`not_run`。
