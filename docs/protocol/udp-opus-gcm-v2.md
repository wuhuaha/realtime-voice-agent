# UDP Opus GCM Media Profile v2

状态：current selectable（发布仍受 HIL/弱网门禁约束）
Profile ID：`udp-opus-gcm-v2`
Control：`rva-control-v2`
Byte authority：`protocol/udp_opus_gcm_v2/README.md` 与 fixtures

## 1. 边界

UDP 只承载媒体与 liveness；`/v2/voice` WSS 始终负责鉴权、profile commit、transcript、response、playback stop、
物理事实和 session lifecycle。v2 不包含 ICE/STUN/TURN、DTLS、RTCP、NACK、RTX 或同 session transport migration。

新 profile 使用 shared header `wire_version=0x02` 并把 generation 从 uplink 完全移除。旧
`udp-opus-gcm-v1`/header `0x01` fail closed。

## 2. Grant 与 datagram

Fresh session 生成 `media_id`、non-zero `media_epoch`、双向独立 AES-GCM key/salt、UDP endpoint、expiry 与 probe
timeout。Key/salt 不记录、不持久化、不跨 session 复用。

```text
32-byte header | ciphertext(payload_length) | 16-byte GCM tag
```

Header layout 与 generation/flag 规则见 canonical README。Datagram 上限 1280 bytes，Opus payload 上限 1200 bytes，
不支持 fragmentation。Wire version、directional generation、flag 与长度均 fail closed。

## 3. Generation 解耦

- Device uplink AUDIO 始终 `generation=0`，播放期间也连续上传 AEC 后 Opus/DTX。
- PROBE、PROBE_ACK、KEEPALIVE 双向始终 `generation=0`。
- 只有 Server downlink AUDIO 携带 exact `response.begin.generation`。
- Endpoint 在 authentication 后校验 generation，并在 decode、queue 与 DAC dequeue 前复核 active target/fence。

Capture/uplink 因而不依赖 playback state；VAD 只可用于 UI、指标或未来新 profile 的协商优化，不能改变本 profile
的 cadence 或播放状态。

## 4. AEAD 与 admission

AES-128-GCM 使用 `directional_salt[8] || sequence_be[4]` nonce，完整 header 作为 AAD，tag 固定 16 bytes。只有
authentication 成功后才能推进 replay window、绑定 source、提交 sequence 或 generation state。

Canonical anti-replay history 为 64 packet，最大 forward jump 为 1024 packet，jitter window 为 4 packet，缺口等待
上限 120 ms 后用 Opus PLC 推进。未认证 packet 不触发 PROBE_ACK；source 绑定后拒绝其他 address，即使 AEAD valid。

## 5. Liveness 与恢复

设备在 probe timeout 内未 ready 则关闭整个 session。设备按 heartbeat interval 发送认证 KEEPALIVE，Server 回送认证
KEEPALIVE；idle timeout 内无认证 UDP packet 时废弃 media session，并通过 fresh bootstrap 新建 WSS profile session。

WSS 断开、grant expiry、sequence exhaustion、source change 或 fatal socket error 都立即撤销 key/source/session。
不得在同 session 静默切换 carrier。

## 6. 验证

Server 与 Firmware 直接消费 `protocol/udp_opus_gcm_v2/fixtures/` 的固定 bytes，覆盖 `wire_version=2`、AAD/tag
tamper、authenticated uplink non-zero generation、non-AUDIO generation、replay、source pin、jitter/loss/PLC、fence、
teardown 和 reconnect。Host/contract 证据不能替代当前 artifact 的双向媒体、弱网、声学与长稳 HIL。
