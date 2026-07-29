# UDP Opus GCM Media Profile v2

状态：current selectable

本目录定义 `rva-control-v2` 选择 `udp-opus-gcm-v2` 后的 canonical byte wire。WSS 始终负责鉴权、
profile commit、session/response/playback lifecycle；UDP 只传媒体和 liveness，不单独恢复 session。

`fixtures/positive.json` 与 `fixtures/negative.json` 是 Server 和 Firmware 共同消费的固定向量。fixture 中的
key、salt 和 payload 都是公开测试材料，绝不能用于部署。

## Datagram

```text
32-byte header | AES-GCM ciphertext (payload_length bytes) | 16-byte tag
```

所有多字节整数使用 network byte order。Header 是 packed layout：

| Offset | Bytes | Field | v2 rule |
| ---: | ---: | --- | --- |
| 0 | 2 | `magic` | ASCII `VA` |
| 2 | 1 | `wire_version` | 固定 `0x02`；`0x01` fail closed |
| 3 | 1 | `flags` | 只允许一个合法 flag |
| 4 | 8 | `media_id` | 匹配 `session.opened` |
| 12 | 4 | `media_epoch` | 匹配 `session.opened` 的 non-zero `uint32` |
| 16 | 4 | `sequence` | 各方向独立、从 0 严格递增 |
| 20 | 4 | `timestamp` | 16 kHz clock；PROBE/PROBE_ACK 为 0 |
| 24 | 4 | `generation` | uplink 和 non-AUDIO 固定 0；downlink AUDIO 为 active response generation |
| 28 | 4 | `payload_length` | 明文/密文长度，不含 tag |

Flags：AUDIO=`0x01`、PROBE=`0x02`、PROBE_ACK=`0x04`、KEEPALIVE=`0x08`。不允许组合 flag。
AUDIO payload 是一个完整 Opus packet，范围 1..1200 bytes；其他 flag payload 必须为空。Datagram 上限
1280 bytes，不支持 fragmentation。

## Generation

Generation 只属于 downlink playback。设备所有 uplink AUDIO 即使在 Server 播放期间也固定发送 `0`，避免
capture admission 与 playback state 耦合。PROBE、PROBE_ACK、KEEPALIVE 双向固定 `0`。只有 downlink AUDIO
携带 `response.begin.generation`；设备在 decode、queue 和 DAC dequeue 前均复核 exact target/fence。

这项语义和 header `wire_version=2` 共同构成 v2 profile。实现不得把 v1 packet 当 v2 接受，也不得用 profile
协商去绕过 byte discriminator。

## AEAD

- AES-128-GCM，上下行使用独立 16-byte key 和 8-byte salt。
- Nonce 为 `directional_salt[8] || sequence_be[4]`。
- AAD 是未经修改的完整 32-byte header；tag 固定 16 bytes。
- 只有 authentication 成功后才能推进 anti-replay、绑定 source 或提交 generation/sequence state。
- Sequence 不得回绕；耗尽前关闭并 fresh bootstrap。
- PROBE ACK 总 deadline 内，Endpoint 可以每 250 ms 重发首次生成的 byte-identical sequence-zero PROBE；重发
  不得延长 deadline，也不得在同一 nonce 下生成不同 ciphertext/AAD。Server 仅在已绑定同一 source、首个后续
  uplink 尚未接收、原 replay entry 仍在窗口、握手时间/次数预算未耗尽时重新认证。通过后只重发缓存的同一个
  byte-identical sequence-zero PROBE_ACK；不得推进任一方向 sequence/replay、媒体 cursor 或 liveness。

## Admission

1. 校验 datagram/header length、magic、`wire_version=2`、单一 flag、payload length 和 MTU。
2. 定位 `media_id/media_epoch`，预检查 64-packet replay history 与 1024-packet 最大 forward jump。
3. 使用 header 作为 AAD 完成 GCM authentication。
4. 校验 source binding；首次认证 PROBE 绑定 endpoint 并返回 PROBE_ACK。
5. 在严格握手窗口内，对同一 bound source 的 byte-identical sequence-zero PROBE 允许重新认证，并限频重发缓存的
   byte-identical ACK；重复包不得二次提交 replay、sequence 或 liveness。
6. 校验 generation：uplink/non-AUDIO 必须为 0；downlink AUDIO 必须 exact-match active target。
7. 通过 4-slot jitter admission 后提交 sequence；缺口最多等待 120 ms，随后用 Opus PLC 推进 live edge。

未认证 packet 不得推进任何可观测 session state，也不得触发放大响应。WSS 断开、grant expiry、source 变化、
sequence exhaustion 或 fatal media error 都撤销 UDP session；恢复必须 fresh bootstrap，不做同 session transport switch。

## Fixture consumption

跨语言测试必须直接校验 `header_hex`、`nonce_hex`、`ciphertext_and_tag_hex` 与 `datagram_hex`。Negative fixture
中的 `reject_stage` 区分 framing、authentication 与 authenticated generation policy；不得在各 endpoint 维护第二份
expected bytes。
