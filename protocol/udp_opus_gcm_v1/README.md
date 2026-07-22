# UDP Opus GCM Media Profile v1

状态：current wire contract，profile 名称为 `udp-opus-gcm-v1`。

本目录只定义 WSS 控制会话建立后的 UDP 媒体封装。WSS 仍负责鉴权、
transport selection、session 生命周期和 ASR/TTS/abort 控制消息。UDP 不单独恢复
session；WSS 断开后必须废弃对应 UDP key 和 session。

`fixtures/positive.json` 和 `fixtures/negative.json` 是 Python 与 ESP32 C++ 实现共享的
固定测试向量。其中 key、salt 和 payload 均为公开测试材料，绝不能用于部署。

## Datagram

```text
32-byte header | AES-GCM ciphertext (payload_length bytes) | 16-byte tag
```

所有多字节整数使用 network byte order（big-endian）。header 使用 packed layout，不能有
编译器 padding。

| Offset | Bytes | Field | v1 rule |
|---:|---:|---|---|
| 0 | 2 | `magic` | ASCII `VA` |
| 2 | 1 | `version` | `0x01` |
| 3 | 1 | `flags` | 下表中的单个 flag |
| 4 | 8 | `media_id` | WSS grant 下发的 opaque bytes |
| 12 | 4 | `media_epoch` | WSS grant 下发的 non-zero `uint32` |
| 16 | 4 | `sequence` | 每个方向独立、从 0 开始递增的 `uint32` |
| 20 | 4 | `timestamp` | 16 kHz Opus sample clock；probe 使用 0 |
| 24 | 4 | `generation` | playback generation；旧 generation 不得进入播放 |
| 28 | 4 | `payload_length` | 明文/密文长度，不包含 16-byte tag |

Flags：

| Value | Name | Direction | Payload |
|---:|---|---|---|
| `0x01` | `AUDIO` | 双向 | 单个完整 Opus packet，1..1200 bytes |
| `0x02` | `PROBE` | device -> server | empty |
| `0x04` | `PROBE_ACK` | server -> device | empty |
| `0x08` | `KEEPALIVE` | 双向 | empty |

v1 不允许组合 flags。datagram 上限为 1280 bytes，不支持 fragmentation。发送端的 Opus
payload 上限为 1200 bytes；接收端必须拒绝超过自身上限或长度不一致的包。

设备按控制面下发的 `heartbeat_interval_ms` 发送经过认证的 `KEEPALIVE`；Server 对有效
`KEEPALIVE` 返回同 session 的经过认证的 `KEEPALIVE`。设备在 `idle_timeout_ms` 内未收到任何
经过认证的 UDP 包时必须废弃 UDP session，并通过仍存活的 WSS 控制连接重新建立为 WSS profile。

## AEAD

- 算法：AES-128-GCM。
- 上下行使用不同的 16-byte key 和 8-byte salt。
- nonce：`directional_salt[8] || sequence_be[4]`，共 12 bytes。
- AAD：未经修改的完整 32-byte wire header。
- authentication tag：完整 16 bytes，附在 ciphertext 后。
- 只有 GCM authentication 成功的包才能推进 anti-replay window 或绑定 UDP source。
- sequence 不得回绕；耗尽前关闭 session 并通过 WSS 获取新 grant。

WSS grant 中 key/salt 使用 base64，`media_id` 使用 16 字符 hex；转换为 wire bytes 后再参与
封包。每个新 session 必须产生新的双向 key、salt、`media_id` 和 `media_epoch`。

## Admission order

接收端按以下顺序处理，失败即静默丢弃并计数：

1. 检查 datagram/header 长度、magic、version、payload length 和 MTU。
2. 检查 `media_id`、`media_epoch`、允许的单一 flag 和 sequence replay window。
3. 用 header 作为 AAD 完成 GCM authentication。
4. 检查已绑定 source；首次通过认证的空 `PROBE` 绑定 source 并返回 `PROBE_ACK`。
5. authentication 成功后提交 sequence；audio 再进入 bounded reorder/jitter queue。

`generation` 是 freshness fence，不是加密计数器。收到更高 generation 时应清空旧播放
媒体；低于当前 generation 的下行 audio 必须丢弃。

## Fixture consumption

跨语言测试必须直接读取 fixture 中的 `header_hex`、`nonce_hex`、
`ciphertext_and_tag_hex` 和 `datagram_hex`，并验证逐字节相等。不得在测试中用另一份字段
布局重新生成 expected bytes。negative fixture 的 `reject_stage` 区分 framing parser 与 GCM
authentication，便于 Python 和 ESP32 得到一致的拒绝行为。
