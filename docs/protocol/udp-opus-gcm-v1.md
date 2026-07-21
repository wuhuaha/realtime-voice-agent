# UDP Opus GCM Media Profile v1

状态：challenger
Profile ID：`udp-opus-gcm-v1`

## 1. 边界

UDP 只传媒体，WSS 始终负责鉴权、profile commit、grant、ASR/TTS/abort 和 session lifecycle。它是针对
TCP HOL 的受控 challenger，不是缩小版 WebRTC，不包含 ICE/STUN/TURN、SDP、DTLS、RTCP、NACK、RTX、
GCC、Room/SFU 或 mid-session NAT migration。

字节级权威为 `protocol/xiaozhi_udp_v1/README.md` 和其 positive/negative fixtures。

## 2. Grant

每个 fresh session 生成：`media_id`、non-zero `media_epoch`、`uplink_key/salt`、`downlink_key/salt`、
UDP host/port、expiry、probe timeout 和 wire limits。Key 为 16 bytes，salt 为 8 bytes；control 使用 base64，
`media_id` 使用 16-char hex。Grant 不得写日志、持久化或跨 session 复用。

## 3. Datagram

```text
32-byte header | ciphertext(payload_length) | 16-byte GCM tag
```

| Offset | Bytes | Field | Rule |
| ---: | ---: | --- | --- |
| 0 | 2 | magic | ASCII `VA` |
| 2 | 1 | version | `0x01` |
| 3 | 1 | flags | 单一 `AUDIO/PROBE/PROBE_ACK/KEEPALIVE` |
| 4 | 8 | media_id | opaque session media id |
| 12 | 4 | media_epoch | non-zero, network byte order |
| 16 | 4 | sequence | 每方向独立，从 0 递增 |
| 20 | 4 | timestamp | 16 kHz Opus sample clock；probe 为 0 |
| 24 | 4 | generation | playback freshness fence |
| 28 | 4 | payload_length | 不含 tag |

Datagram 上限 1280 bytes，Opus payload 上限 1200 bytes，不支持 fragmentation。KEEPALIVE payload 必须为空；
timestamp 可为非零。Sequence 不得回绕，耗尽前关闭并重新建立 session。

## 4. AEAD

- 算法：AES-128-GCM。
- Nonce：`directional_salt[8] || sequence_be[4]`。
- AAD：未经修改的 32-byte header。
- Tag：完整 16 bytes。
- 上下行 key/salt 独立。

只有 authentication 成功后才能提交 replay window、绑定 source 或进入 decoder。Header 篡改、错 key、错
epoch、重复 sequence 和超限 payload 静默丢弃并计数。

## 5. Admission 顺序

1. 校验 datagram/header length、magic、version、单一 flag、payload length 和 MTU。
2. 定位 `media_id/media_epoch`，预检查 64-packet replay/reorder window。
3. 使用 header 作为 AAD 完成 GCM authentication。
4. 校验已绑定 source；首次通过认证的空 PROBE 绑定 endpoint 并返回 PROBE_ACK。
5. Authentication 成功后提交 sequence。
6. AUDIO 进入有界 reorder/jitter；旧 generation 在 decode/playout 前拒绝。

未认证包不得触发 PROBE_ACK，避免 amplification。Source 已绑定后，其他 address 即使 auth 成功也拒绝；
NAT/Wi-Fi 改变使用 fresh session，不做 rebinding。

## 6. Jitter、loss 与 PLC

首版使用固定小型窗口：

- 允许 64 packet anti-replay/reorder 视窗，但 playout 等待仅采用可配置小 deadline。
- 缺口到 deadline 后计为 lost，推进 live edge；不请求重传。
- Receiver 应调用 Opus PLC 生成缺失的 60 ms，且保持 1 至 2 帧量级 PCM/playout queue。
- Late packet、queue full、media age 超限和旧 generation 直接丢弃。
- FEC 初始关闭；20/40/60 ms 与 in-band FEC 只作为独立实验变量。

## 7. Liveness 与关闭

- UDP 必须在 probe timeout 内 ready，否则整个 session 关闭并 fresh reopen。
- WSS 断开、grant expiry、sequence exhaustion、UDP socket fatal error 或 Worker drain 立即撤销 media session。
- KEEPALIVE 是预留 liveness signal，不替代 WSS lifecycle。
- `auto` 不得在 UDP 失败时同 session 偷切 WSS；新 bootstrap 可以按策略选择 WSS。

## 8. 观测

每 session 至少记录 received/authenticated/invalid/replayed/wrong_source/queue_dropped/sent/lost/reordered/late、
max media age、probe time 和 generation drops。不得记录 key、完整 datagram 或原始 Opus。

## 9. 验证边界

必须覆盖 canonical fixtures、tamper、replay、forward jump、wrong source、probe、generation、late/loss/PLC、
teardown 和重连。Host/contract 不能替代当前 native artifact 的双向媒体、弱网、声学和长稳 HIL；状态见
[Release readiness](../quality/release-readiness.md)。门禁通过前 UDP 不成为无条件默认 profile。
