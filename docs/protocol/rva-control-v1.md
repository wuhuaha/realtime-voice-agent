# RVA Control Protocol v1

状态：implementing
Protocol ID：`rva-control-v1`
Machine-readable authority：`protocol/rva_control_v1/contract.yaml`、`messages.schema.json`

## 1. 边界

`rva-control-v1` 是 Product 自有的设备会话控制协议，运行在 `/v1/voice` WebSocket 上。它只负责 session、
字幕、响应生命周期、精确取消、错误和关闭；Opus 媒体由本次会话选定的 `wss-opus-v2` 或
`udp-opus-gcm-v1` 承载。

它不包含 Xiaozhi `hello/listen/tts/abort/mcp` 兼容事件，不承担 OTA、MQTT、设备激活或业务工具协议。

## 2. 建连与鉴权

1. 设备向 Director bootstrap，声明 `control_protocol=rva-control-v1` 和支持的 media profiles。
2. Director 返回绑定 `/v1/voice`、Worker、device、session epoch、profiles 和有效期的单次 connect grant。
3. 设备使用 grant 建立 WSS；Worker 必须在接受业务消息或媒体前验证并消费 grant。
4. 设备在 handshake timeout 内发送唯一 `session.open`。
5. Worker 返回 `session.opened`，commit 一个 `selected_media_profile`、session/media identity 和运行 limits。

同一 WebSocket 只承载一个 session。鉴权失败、grant replay、device/binding 不匹配或重复 `session.open` 都是
fatal protocol error；恢复方式是 fresh bootstrap，不是复用旧连接。

## 3. 消息与方向

| Type | Direction | 作用 |
| --- | --- | --- |
| `session.open` | device -> server | 提交 device capability、支持和偏好的 media profile |
| `session.opened` | server -> device | 建立 session/media identity，并选定唯一 profile |
| `transcript.delta` | server -> device | 一个 utterance 内按 sequence 发送流式 ASR 文本 |
| `transcript.final` | server -> device | 终止当前 utterance 的 transcript sequence |
| `response.begin` | server -> device | 打开新的 playback generation |
| `response.text` | server -> device | 一个 response 内按 sequence 发送流式助手文本 |
| `response.end` | server -> device | 正常或失败地终止 response generation |
| `response.cancel` | device -> server | 精确取消 active `response_id + generation` |
| `response.cancelled` | server -> device | 确认目标 generation 已被 fence 并终止 |
| `session.error` | bidirectional | 报告有界、分类后的 session 错误 |
| `session.close` | bidirectional | 终止当前 session；之后不再接收控制或媒体 |

JSON 必须通过 `messages.schema.json`。未知消息、未知或重复字段、类型错误、越界文本、嵌入 NUL 和超过
32 KiB 的 control message 必须 fail closed。

## 4. Identity、sequence 与 generation

- `session_epoch` 是 Director 生成的不透明字符串；逻辑 session 重建时改变。
- `media_epoch` 是大于零的 `uint32`；媒体 admission 重建时改变。
- 所有 post-open control 必须匹配 active `session_id + session_epoch`。
- 每个 media packet 必须匹配 `media_id + media_epoch`。
- transcript 和 response text 的 sequence 在各自 scope 从 0 严格递增；不接受重复或倒退。
- `response.begin` 打开单一 active generation；generation 必须单调增加且大于零。
- `response.cancel` 的 `response_id` 和 `generation` 必须同时匹配 active response。
- cancel 先推进 playback fence、清空旧 decoder/playout queue，再调用 Agent interrupt；迟到 callback 不得恢复旧输出。
- generation 的 carrier 规则由 media profile 定义：WSS uplink 固定 0；UDP audio 双向携带当前 playback generation，
  UDP probe 使用 0。
- sequence 即将回绕时关闭并 fresh reopen，不实现 mid-session reset。

## 5. Profile commit 与媒体

`preferred_media_profile` 必须同时出现在 `supported_media_profiles` 中；Server 只能从设备报价与 connect grant
允许集合的交集中选择。`session.opened.selected_media_profile` 一旦返回，本 session 不允许 transport switch。

共享媒体参数固定为 Opus 16 kHz、mono、60 ms、960 samples/frame、DTX on、FEC off。媒体 header、载荷上限和
profile-specific admission 见 `contract.yaml` 及对应媒体文档。

## 6. 关闭与恢复

`session.close` 是 terminal。WSS 断开同时撤销该 session 的媒体身份、UDP key/source binding、active response
和未完成 provider output。网络变化、idle timeout、协议错误或 Worker drain 后，设备执行有界 teardown，再重新
bootstrap；不恢复旧 turn，不迁移旧 generation。

## 7. 实现门禁

- Server 与 Firmware parser 必须共同消费 canonical fixtures，不维护第二份字段定义。
- 必测负向路径：oversize、unknown/duplicate field、stale session/media identity、sequence 回退、generation 复活、
  cancel target 不匹配、terminal 后输入和 grant replay。
- host/contract test 不能替代真实 WSS、provider、设备播放、AEC 或长稳证据。
