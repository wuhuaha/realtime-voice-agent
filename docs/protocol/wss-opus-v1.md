# WSS Opus Media Profile v1

状态：baseline
Profile ID：`wss-opus-v1`

## 1. 适用边界

WSS profile 面向防火墙兼容、实现简单和当前 ESP32 稳定基线。它复用 `xiaozhi-control-v1` 的同一连接：text
message 为 JSON control，binary message 为媒体。它不提供 RTP/RTCP、NACK、FEC 协商或独立媒体恢复。

## 2. Framing

- 一个 WebSocket binary message 必须恰好包含一个完整 Opus packet。
- 不允许一个 packet 跨 message fragmentation，也不允许一个 message 拼接多个 packet。
- Client uplink 固定 16 kHz、mono、60 ms。
- Server downlink sample rate 由 hello commit，v1 为 16 kHz 或 24 kHz、mono、60 ms。
- 空 binary message、超过 Worker hard limit 的 message 或无法解码为合同帧长的 packet 必须拒绝。
- 实现默认 binary admission ceiling 可为 4096 bytes，但应接受合法 Opus 最大包；收紧必须进入兼容测试。

## 3. 顺序与时间

WebSocket 提供有序可靠交付，因此 wire 不增加 sequence/timestamp header。Sender 必须按 60 ms cadence 生产，
receiver 按到达顺序解码。实现仍必须记录 enqueue age、queue depth 和 playout deadline，不能因为 TCP 可靠就
播放已经过时的音频。

TCP packet loss 可能阻塞后续媒体。禁止通过无界 queue 掩盖 HOL：

- uplink queue 超时应关闭或回到 live edge，不积压历史讲话。
- downlink generation 改变立即清空旧 audio queue。
- control 优先级高于普通媒体；持续 send congestion 必须有界关闭。

## 4. Generation 与打断

WSS media 本身不携带 generation。Worker 在内部给每个 outbound item 绑定 generation；发送前再次比较当前
generation。Client 收到更高 generation 的 TTS control、发送 abort 或进入新 session 时必须清空 decoder、
jitter/playout queue。已经进入底层 socket 的单个旧 message 可能不可取消，Client 状态门禁必须拒绝其播放。

## 5. Lifecycle

1. WSS 鉴权成功。
2. Client hello capability 包含 WSS。
3. Server hello commit `wss-opus-v1`。
4. `listen start` 后接收 uplink binary media。
5. TTS control 与 downlink binary media 按同一连接发送。
6. 任一 transport/protocol/runtime fatal error 关闭整个 session；fresh reconnect。

WSS ping/pong 只证明连接活性，不替代媒体 age、AgentSession 或 provider health。

## 6. 安全

- 生产只允许 `wss://` 和受信证书。
- 鉴权在接受 hello/media 前完成。
- WebSocket compression 默认关闭，避免实时抖动和敏感上下文压缩风险。
- 限制 max message、max queue、handshake timeout、ping interval/timeout 和单 principal session 数。

## 7. 观测与验收

至少记录 packets/bytes、decode errors、queue drops、max media age、send wait、playback underrun、interrupt tail
和 close reason。WSS 弱网结果必须与 UDP 在相同 codec/provider/网络模型下比较。

该 profile 仅作为 compatibility baseline 保留，当前发布证据见 [Release readiness](../quality/release-readiness.md)。
