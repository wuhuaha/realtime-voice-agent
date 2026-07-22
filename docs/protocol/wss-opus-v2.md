# WSS Opus Media Profile v2

状态：current default
Profile ID：`wss-opus-v2`
Control：`rva-control-v1`

## 1. 适用边界

`wss-opus-v2` 在 `/v1/voice` 的同一 WSS 上传输 JSON control 和 typed binary Opus。与 v1 的裸 Opus message
不同，v2 每个 binary message 都携带共享 32-byte media header，因此设备可以在 decode/render 前执行
session、media epoch、sequence 和 generation fence。

该 profile 以实现简单和网络可达性为优先；它仍会受 TCP head-of-line blocking 影响，因此不能用无界 queue
换取表面上的无丢包。

## 2. Binary framing

每个 WebSocket binary message 恰好包含：

```text
32-byte RVA media header | one complete Opus packet
```

Header 使用 network byte order：

| Offset | Bytes | Field | 规则 |
| ---: | ---: | --- | --- |
| 0 | 2 | `magic` | 固定 `0x5641` (`VA`)，与 canonical `udp-opus-gcm-v1` 共享 |
| 2 | 1 | `wire_version` | 固定 `1` |
| 3 | 1 | `flags` | audio=`0x01`；WSS 媒体只接受单一合法 flag |
| 4 | 8 | `media_id` | 必须匹配 `session.opened` |
| 12 | 4 | `media_epoch` | 必须匹配 `session.opened`，且大于零 |
| 16 | 4 | `sequence` | 各方向独立，从 0 严格递增 |
| 20 | 4 | `timestamp` | 16 kHz media clock，32-bit wrap 前 reopen |
| 24 | 4 | `generation` | uplink 固定 0；downlink 为 active generation |
| 28 | 4 | `payload_length` | 必须等于 binary message 剩余长度 |

Opus payload 上限 1200 bytes，完整 message 上限 1232 bytes。不允许拼接多个 packet；WebSocket 库产生的
fragment 必须由有界 assembler 按连续 offset 重组后再进入 parser。

## 3. Admission 顺序

接收端按以下顺序处理，失败立即拒绝，不能部分推进状态：

1. 检查 message/fragment 长度和完整性。
2. 检查 magic、version、flags、payload length。
3. 检查 `media_id + media_epoch`。
4. 检查方向对应的 generation 规则。
5. 检查 sequence。
6. 检查 media age 和当前 playback fence。
7. 最后才把 Opus payload 交给 decoder/Agent input。

TLS/WSS 提供链路认证与保密，v2 不在 binary payload 内重复加密。生产不得使用明文 `ws://`；受控本地开发
例外必须显式配置。

## 4. 实时队列与 pacing

- callback 只复制完整、有上限的数据并入队；不得在 WebSocket callback 内 teardown、decode 或阻塞等待。
- supervisor 是连接 close/destroy 的唯一 owner。
- uplink input、Agent output、socket output 和 PCM playout 都必须有固定容量与超时。
- downlink 以 60 ms cadence pacing；首包可小幅 prebuffer，但不得持续积累旧音频。
- queue 满、send congestion 或 media age 持续超限时，优先丢弃已过时 generation；无法回到 live edge 时关闭 session。
- cancel/generation 改变必须唤醒 pacing wait，并在下一次 send/render 前复核 fence。

## 5. Codec 与时间

Opus 固定 16 kHz、mono、60 ms、960 samples/frame、DTX on、FEC off。Sender 每包 timestamp 增加 960；
receiver 不用 WebSocket 到达时间替代 media timestamp。首版不做 NACK、RTX、RTP、RTCP 或同 session transport
migration。

## 6. 观测与验收

至少记录 packets/bytes、invalid/stale media、queue depth/drop、enqueue age、send wait、decode error、playout
underrun、cancel tail 和 close reason。必须分别验证：

- canonical header 正反向 fixtures 与 parser negative cases；
- socket fragmentation、queue overload、disconnect 和 bounded cleanup；
- synthetic Opus 端到端进入 Agent/返回下行；
- 真机 ASR、字幕、TTS、点击/近讲打断与 30 分钟长稳。

当前 contract、Python binding/runtime 和 Firmware host parser 已有 focused evidence；native app、真实网络和 HIL
完成前仍保持 `implementing`，不得标为 `device_verified`。
