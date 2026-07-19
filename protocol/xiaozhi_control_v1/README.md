# Xiaozhi Control Protocol v1

状态：当前 ESP32 endpoint 与 Realtime Worker 的 canonical control contract。

## 建连

- WebSocket path：`/v1/xiaozhi`
- `Protocol-Version: 1`
- `Device-Id`：物理设备标识；`Client-Id` 仅作兼容 fallback。
- `Authorization: Bearer <credential>`：可为兼容 lab token 或 Director 下发的 worker-bound grant。
- 文本帧必须是 UTF-8 JSON object；重复 key、超限、错误 session 或不支持的状态 fail closed。

客户端首先发送 `hello`。`transport_profiles` 省略时仅表示 `wss-opus-v1`；`transport_mode` 省略时为
`auto`。云侧从设备 capability、设备 veto、server policy 与 UDP readiness 交集中选择一个 profile。
当前 `auto` 保守选择 WSS；UDP 仍为显式 challenger。

服务端返回唯一 `session_id` 和选中的 `transport_profile`。WSS profile 的二进制帧直接承载单个完整 Opus
packet；UDP profile 的二进制媒体只走 UDP，WSS 继续承载控制、ASR/TTS 文本、abort 和生命周期。

## Fresh session

- 一个 fresh session 只有一个 worker、一个 media owner、一个 roomless LiveKit `AgentSession` 和一个
  playback generation owner。
- WSS 断开、UDP session 失活、worker/lease 失效或网络切换时关闭整个 session；重新 bootstrap/连接。
- 不做进行中 turn 的 transport 切换、跨 worker 迁移、same-session rebuild 或旧媒体恢复。
- TTS abort 递增 generation；旧 generation 的排队或到达音频必须被清空/拒绝。

Schema 位于 `messages.schema.json`，正反向 fixture 位于 `fixtures/`。Schema 是 wire 边界，不代表所有可选
Xiaozhi MCP 业务 payload；MCP 仍按 opaque bounded JSON 转发，不能绕过大小与 session 检查。
