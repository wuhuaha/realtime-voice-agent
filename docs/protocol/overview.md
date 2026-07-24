# 协议总览

状态：accepted
更新日期：2026-07-23

## 1. 当前协议

Product runtime 只接受一套 current wire：

```text
rva-control-v2 over /v2/voice WSS
  + wss-opus-v3        current default
  + udp-opus-gcm-v2    authenticated selectable profile
```

不提供 v1、Xiaozhi 或新旧 media profile dual stack。旧 wire 由 Git 历史和 migration/ADR 保存，不在当前 registry、
schema、fixture、runtime route 或发布资产中继续 authoring。

## 2. 权威层级

1. `protocol/registry.yaml` 定义唯一 current control/profile ID、path 与 wire references。
2. `protocol/rva_control_v2/contract.yaml` 定义 control state、shared media header 和 directional generation。
3. `protocol/rva_control_v2/messages.schema.json` 与 control fixtures 定义 strict JSON surface。
4. `protocol/udp_opus_gcm_v2/README.md` 与 fixtures 定义 UDP byte wire、AEAD 和拒绝阶段。
5. 本目录解释 lifecycle、物理播放事实、安全和验证边界。
6. Python/C++ 实现、示例和部署配置不得反向改变 canonical wire。

任何冲突必须修正权威 artifact 或消费者，不能在 endpoint 内维护第二份字段/profile 定义。

## 3. Profile

| ID | Control | Carrier | Header | Uplink generation | 状态 |
| --- | --- | --- | --- | ---: | --- |
| `wss-opus-v3` | `rva-control-v2` | 同一 WSS binary | shared `0x02` | 0 | current default |
| `udp-opus-gcm-v2` | `rva-control-v2` | AES-128-GCM UDP | shared `0x02` | 0 | current selectable |

Codec 固定 Opus 16 kHz、mono、60 ms、960 samples/frame、DTX on、FEC off。只有 downlink AUDIO 携带 active
response generation；WSS/UDP uplink 与 UDP non-AUDIO 固定 0。

## 4. 协商

设备 `session.open(protocol_version=2)` 提交唯一、有序的 `supported_media_profiles`、位于其内的 preference、固定
audio 参数和 endpoint capabilities。Worker 只从 device offer、connect grant allow-list、server policy 与 readiness
交集选择，并在 `session.opened` commit 一个 profile。

WSS 是 session root。选择 UDP 时必须同时返回完整 per-session directional grant；profile commit 后禁止同 session
transport switch。UDP 不 ready 或网络变化时关闭并 fresh bootstrap，不偷切 WSS。

## 5. Response 与 playback

- Server 为一个语义 response 分配一个 `response_id + generation`；多次 TTS flush 不产生新 generation。
- `response.end` 是 Server generation 的唯一 terminal，outcome 为 `completed/cancelled/failed`。
- `completed` 用 `final_media_sequence` 定义 drain 边界；`failed` 使用 strict `error_code`；`cancelled` 不携带二者。
- Server 以 `playback.stop(target, fence_generation, cause)` 成为 stop/fence 权威。
- Endpoint 只执行 exact target 的物理 stop，并回报 response 级唯一 `playback.started/ended`。
- `playback.ended.played_samples` 是 DAC-near 物理证据；Server send、provider complete、flush 或 queue empty 都不能代替。

## 6. 打断职责

播放期间 endpoint 持续上传 AEC 后 Opus/DTX，Server 的 streaming ASR 和 `InterruptionCoordinator` 是语音打断接受
的唯一权威。端侧 VAD/wake 不改变 playback state，也不发送 acoustic cancel。

物理按钮等明确用户操作可先 local hard stop，再发送 exact `response.cancel.request`。该请求只允许
`cause=user_request`，并用 `request_id` 提供 session-epoch 内幂等；重复请求不得重复 stop/terminal 副作用。

## 7. Session 与安全

- Fresh session 使用新的 `session_id/session_epoch/media_id/media_epoch` 和 grant/key。
- 所有 post-open control 与 media 必须 exact-match active identity。
- Shared media header `wire_version=0x02`；旧 version fail closed。
- Production WSS 使用 TLS；UDP 使用每 session、每方向独立 AES-128-GCM key/salt、完整 tag、anti-replay 和 source pin。
- 未认证 UDP packet 不推进 sequence、generation、source binding 或 decoder state。
- 所有输入有硬长度/容量上限；日志不输出 token、key、完整 URL query、原始音频或无界 payload。
- `session.close` 与 WSS disconnect 撤销所有 session/media/output state；无法交付的 physical outcome 保持 unknown。

## 8. 证据

Schema、control fixtures、UDP byte fixtures 和 Python/C++ consumers 必须通过 contract/host gate；真实 WSS/UDP、
播放期 ASR、semantic interruption、DAC-near facts、弱网与长稳仍需要当前 artifact 的 HIL/acoustic evidence。

详见：

- [RVA Control v2](rva-control-v2.md)
- [WSS Opus v3](wss-opus-v3.md)
- [UDP Opus GCM v2](udp-opus-gcm-v2.md)
- [生命周期与错误](lifecycle-errors.md)
