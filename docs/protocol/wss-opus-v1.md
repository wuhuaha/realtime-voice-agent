# WSS Opus Media Profile 1.0

状态：current default
Profile ID：`wss-opus/1`
Control：`rva/1`

## 1. 边界

`wss-opus/1` 在 `/rva/v1/voice` 同一 WSS 上传输 JSON control 与 typed binary Opus。每个 binary message 携带
32-byte shared media header。该 profile 使用 `wire_version=0x01`，并把 uplink generation 固定为 `0`；其他 profile
或 wire version 均不接受。

## 2. Framing

```text
32-byte RVA media header | one complete Opus packet
```

| Offset | Bytes | Field | 规则 |
| ---: | ---: | --- | --- |
| 0 | 2 | `magic` | 固定 `0x5641` (`VA`) |
| 2 | 1 | `wire_version` | 固定 `0x01` |
| 3 | 1 | `flags` | 固定 AUDIO=`0x01` |
| 4 | 8 | `media_id` | 匹配 `session.opened` |
| 12 | 4 | `media_epoch` | 匹配 `session.opened` |
| 16 | 4 | `sequence` | 各方向独立，从 0 严格递增 |
| 20 | 4 | `timestamp` | 16 kHz media clock |
| 24 | 4 | `generation` | uplink 固定 0；downlink 为 exact active response generation |
| 28 | 4 | `payload_length` | 等于 message 剩余长度 |

Opus payload 上限 1200 bytes，完整 message 上限 1232 bytes。一个 binary message 恰含一个 Opus packet；fragment
必须由有界 assembler 按连续 offset 重组后才进入 parser。

## 3. Admission

接收端按 message length、magic/version/flag/payload length、media identity、directional generation、sequence、
media age 的顺序校验，最后才进入 Agent input 或 decoder。Downlink 还必须在 queue 与每次 DAC dequeue 前复核
exact target 和当前 fence；cancel 后迟到 packet 不能恢复旧 generation。

TLS/WSS 提供链路认证和保密，binary payload 不重复加密。生产必须使用 `wss://`；明文只允许显式受控本地开发。

## 4. Uplink 与队列

设备按连续 60 ms cadence 上传 AEC 后 Opus，静音使用 DTX；Server 正在播放时也不 suppress input。Uplink
generation 始终为 0，因此 capture task 不读取、不推进 playback generation，也不 reset playback queue。

所有 callback、socket output、decoder、PCM playout 与 control command lane 都有固定容量和超时。Stop command
必须优先于普通媒体，唤醒 pacing wait，并在 DAC-near gate 生效；积压无法恢复 live edge 时关闭 session。

## 5. Codec 与证据

Opus 固定 16 kHz、mono、60 ms、960 samples/frame、DTX on、FEC off。Sender 每包 timestamp 加 960；sequence 或
timestamp 回绕前 fresh bootstrap。

必须验证 header 正负样本、uplink generation zero、socket fragmentation、overload、disconnect、exact fence、
response-level `playback.started/ended` 与真实播放期 ASR。Server send/flush 和 queue empty 都不能替代 device physical fact。
