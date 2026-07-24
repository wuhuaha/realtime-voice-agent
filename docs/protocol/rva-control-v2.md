# RVA Control Protocol v2

状态：current
Protocol ID：`rva-control-v2`
Machine-readable authority：`protocol/rva_control_v2/contract.yaml`、`messages.schema.json`

## 1. 边界

`rva-control-v2` 是 Product 唯一当前设备控制协议，运行在 `/v2/voice` WSS。它负责 session、transcript、
response generation、authoritative playback stop、设备物理播放事实、错误和关闭；Opus 媒体由本次 session 选定的
`wss-opus-v3` 或 `udp-opus-gcm-v2` 承载。

v2 是 clean-slate wire，不接受 `response.cancel`、`response.cancelled`、`barge_in`、`new_wake` 或 Xiaozhi
`hello/listen/tts/abort/mcp`。Product runtime 不实现 v1/v2 dual stack；旧协议只存在于 Git 历史和迁移记录。

## 2. 建连

1. 设备向 Director bootstrap，声明 `control_protocol=rva-control-v2` 以及 v2 media profiles。
2. Director 返回绑定 Worker、device、session epoch、profiles、expiry 和 `/v2/voice` 的单次 connect grant。
3. Worker 验证并消费 grant 后，设备在 handshake timeout 内发送唯一 `session.open(protocol_version=2)`。
4. Worker 返回 `session.opened`，commit 唯一 media profile、session/media identity 和运行 limits。

Director bootstrap REST path 不属于本 control wire；本次只升 Worker WSS path。一个 WSS 只承载一个 session。
鉴权失败、grant replay、重复 open 或 binding 不匹配均 fail closed，恢复必须 fresh bootstrap。

## 3. 消息

| Type | Direction | 作用 |
| --- | --- | --- |
| `session.open` | device -> server | 声明 v2 profile、audio 与 endpoint capability |
| `session.opened` | server -> device | 建立 session/media identity 并 commit 唯一 profile |
| `transcript.delta/final` | server -> device | 发送一个 utterance 的流式/最终 ASR 文本 |
| `response.begin` | server -> device | 为一个语义 response 打开唯一 `response_id + generation` |
| `response.text` | server -> device | 在同一 response scope 内发送文本 delta |
| `response.end` | server -> device | 唯一 response terminal：`completed/cancelled/failed` |
| `playback.stop` | server -> device | 对 exact target 推进 authoritative fence 并要求物理停播 |
| `playback.started` | device -> server | 首个 sample 跨过 DAC-near gate 的物理事实 |
| `playback.ended` | device -> server | drain、stop 或 failure 的唯一物理 terminal fact |
| `response.cancel.request` | device -> server | 只上报明确用户操作对 exact target 的取消请求 |
| `session.error` | bidirectional | 有界、分类后的 session error |
| `session.close` | bidirectional | 终止 session；之后不接受控制或媒体 |

所有 JSON 必须通过 schema。未知消息/字段、重复 key、类型错误、嵌入 NUL、越界值和超过 32 KiB 的 control
message 在产生副作用前拒绝。

## 4. Response 与播放事实

一次 Agent speech/语义回复只分配一个 target。内部 TTS/`AudioOutput.flush()` 只结束媒体 segment，不分配新
generation，也不触发 `playback.started/ended`。

`response.end` 的 outcome 是互斥 union：

| outcome | 必填 | 禁止 | 含义 |
| --- | --- | --- | --- |
| `completed` | `final_media_sequence` | `error_code` | Server 已发送该 target 的最后一个媒体 packet |
| `cancelled` | 无附加字段 | `final_media_sequence`, `error_code` | Server output 已 fence 并终止 |
| `failed` | `error_code` | `final_media_sequence` | Server generation 以稳定错误码失败 |

`response.end` 只描述 Server generation，不能冒充物理播放完成。正常链路中，endpoint 收到
`response.end(completed)` 后继续 drain 到 exact `final_media_sequence`，随后发送一次 `playback.ended(completed)`。

`playback.started` 必须携带 exact `target` 与 `first_media_sequence`，只在首个 sample 实际进入 DAC-near render
boundary 时发送，收到 packet、decode 或入 queue 都不算 started。

`playback.ended` 必须携带 exact `target`、`outcome` 和 `played_samples`：

- `completed` 必须携带 `last_media_sequence`，且等于对应 `response.end.final_media_sequence`；
- `stopped/failed` 可在确实知道最后贡献 sample 的 packet 时携带 `last_media_sequence`；
- 从未播放时 `played_samples=0` 且必须省略 `last_media_sequence`，不能用 `0` 冒充未知 cursor；
- stop-before-first-sample 可以没有 `playback.started`，但 WSS 仍健康时仍产生唯一 `playback.ended`。

`played_samples` 统计跨过声明 render boundary 的 16 kHz mono sample。它是物理证据，不是 Server send、queue 或
provider position。

## 5. Stop 与取消

Server 是语音打断接受的唯一权威。播放期 uplink 音频持续到达 Server；VAD/ASR/interruption policy 命中后，
Server 在短临界区内：

1. 终止 target 的 output admission；
2. 分配严格大于 target generation 和历史 fence 的 `fence_generation`；
3. 优先发送 `playback.stop(target, fence_generation, cause)`，cause 只允许
   `explicit_user_request/recognized_interrupt/session_close/response_failed`；
4. 对该 target 恰发送一次 `response.end(outcome=cancelled|failed)`；
5. 在锁外 bounded interrupt provider/AgentSession。

设备收到 stop 后先原子安装 fence，再关闭 DAC-near gate、清可撤回 decoder/playout queue，并回报物理
`playback.ended`。重复的完全相同 stop 是 no-op；同一 target 的冲突 fence/cause 必须拒绝，且绝不能重定向到
当前 playback。

`response.cancel.request` 仅允许 `cause=user_request`，用于物理按钮等明确操作。Endpoint 可立即 local hard stop，
但必须在 stop 前捕获 exact current target，并发送 `request_id + target`。同一 `request_id` 重试必须内容完全一致且
不重复副作用；复用 request ID 指向其他 target 是 `request_id_conflict`。声学活动、VAD、wake 或任意 transcript
substring 均无权生成该消息。

## 6. Identity、sequence 与 generation

- 所有 post-open control 匹配 active `session_id + session_epoch`；媒体匹配 `media_id + media_epoch`。
- Transcript 与 response text sequence 在各自 scope 从 0 严格递增。
- Media sequence 按方向、按 session 严格递增；回绕前关闭并 fresh bootstrap。
- Generation 由 Server 单调分配，只用于 downlink AUDIO playback identity。
- WSS/UDP 所有 uplink generation 固定 `0`；UDP non-AUDIO 双向固定 `0`。
- Downlink AUDIO generation 必须 exact-match active target；旧、未来或已 fenced generation 在 decode/render 前丢弃。
- 新 profile 的 shared media header `wire_version=0x02`；旧 `0x01` 不接受。

## 7. Media profile

`session.open.supported_media_profiles` 只能包含 `wss-opus-v3`、`udp-opus-gcm-v2`。Preference 必须在 supported
集合中，Worker 只能从 device offer、grant allow-list 和 server policy 的交集选择。Profile commit 后禁止同 session
切换；UDP failure 使用 fresh session 重建为 WSS，不做隐式 fallback。

Codec 固定 Opus 16 kHz、mono、60 ms、960 samples/frame、DTX on、FEC off。Correctness baseline 为连续 uplink
cadence + DTX，使播放期间的 AEC 后音频持续到 Server。未来 VAD-gated uplink 必须用新可协商 profile 定义 pre-roll、
hangover 与 gap，不得在 v2 profile 内静默改变。

## 8. 关闭与验证

`session.close` 是 terminal。WSS 断开立即撤销 UDP key/source、media identity、active response 和 provider output；
无法交付的物理 terminal 在 Server 只能记为 unknown，不能伪造 `playback.ended`。

Server 与 Firmware 必须共同消费 canonical schema/control fixtures 和 UDP byte fixtures。最小门禁包括 strict JSON、
response union、terminal once、cancel idempotency、exact target、fence monotonic、wire version、uplink generation zero、
UDP tamper/replay，以及真实设备 DAC-near started/ended。Contract/host 通过不等于 acoustic/device verified。
