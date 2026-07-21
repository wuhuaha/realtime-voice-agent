# 协议总览

状态：accepted
更新日期：2026-07-21

## 1. 权威层级

迁移期同时提供 legacy 和 Product-native 两组 binding；每个 session 只 commit 一个控制协议和一个媒体 profile：

```text
xiaozhi-control-v1 over WSS
  + wss-opus-v1        baseline
  + udp-opus-gcm-v1    challenger, WSS control retained

rva-control-v1 over WSS
  + wss-opus-v2        native baseline (implementing)
  + udp-opus-gcm-v1    shared challenger (implementing)
```

权威顺序：

1. `protocol/registry.yaml` 定义协议/profile 标识和固定 codec 参数。
2. `protocol/xiaozhi_control_v1/messages.schema.json` 与 fixtures 定义 JSON wire。
3. `protocol/xiaozhi_udp_v1/README.md` 与 fixtures 定义 UDP byte wire 和 AEAD 向量。
4. `protocol/rva_control_v1/contract.yaml`、schema 与 fixtures 定义 RVA control 和 shared media header。
5. 本目录定义状态机、语义、安全、关闭和兼容政策。
6. Python/C++ 实现、README 示例和 HTML 均不得反向改变 wire。

任何冲突必须修正 artifact 或本文，不能在某个 endpoint 内建立第二份隐式协议。

## 2. Profile

| ID | Control | Media carrier | Codec | 状态 |
| --- | --- | --- | --- | --- |
| `wss-opus-v1` | WSS JSON | 同一 WSS binary message | Opus 16 kHz mono 60 ms uplink | baseline |
| `wss-opus-v2` | RVA WSS JSON | 同一 WSS typed binary message | Opus 16 kHz mono 60 ms | implementing |
| `udp-opus-gcm-v1` | WSS JSON | authenticated UDP datagram | Opus 16 kHz mono 60 ms uplink | challenger |

Server output sample rate 由 hello 的 `audio_params.sample_rate` commit，v1 允许 16 kHz 或 24 kHz，mono、
60 ms。一个 session 只能 commit 一个 profile。

## 3. 协商

Client hello 可携带：

- `transport_profiles`：有序、唯一 capability；省略表示仅支持 `wss-opus-v1`。
- `transport_mode`：`auto`、`force_wss` 或 `force_udp_for_test`；省略为 `auto`。
- `features`：兼容 feature map，不得绕过 schema、size 或 state 校验。

Worker 从 device capability、connect grant allowed profiles、server policy、UDP readiness 和设备 veto 的交集选择。
当前 `auto` 保守 commit WSS；UDP 只在显式测试/灰度条件满足时选择。Server hello 返回唯一
`transport_profile`。选择 UDP 时必须同时包含完整 `udp` grant。

## 4. Session 规则

- Fresh session 由新的 `session_id/session_epoch/fencing_token` 标识。
- Client hello 前不接受媒体或业务控制。
- Hello commit 后所有 session-scoped JSON 必须匹配 `session_id`。
- WSS 是 session root；WSS 关闭立即撤销 UDP key/source/session。
- 不支持 mid-session transport switch、Worker migration 或旧 turn 恢复。
- `abort`/interruption 推进 playback generation；旧 generation 输出必须丢弃。
- 失败后重新 bootstrap，不复用过期 grant、UDP key、sequence 或 session id。

## 5. 兼容政策

- `xiaozhi-control-v1` 对未知字段 fail closed；新增可选字段需要 schema、fixture 和双端兼容测试。
- 破坏 required field、语义、framing 或 crypto 的改动必须发布新 protocol/profile id。
- Endpoint 可忽略未协商的 capability，但不得静默降级到未授权 profile。
- `mcp` 是有界 opaque 兼容扩展，不属于核心 schema；跨 endpoint 产品能力不得依赖未版本化 MCP payload。
- WSS 和 UDP 必须分别进行 reference/new 四象限验证。

## 6. 安全摘要

- 生产 WSS 必须使用 TLS；明文 `ws://` 只允许受控本地开发。
- Worker 鉴权优先使用 Director worker-bound 短期 grant；共享 lab token 只用于明确标记的开发环境。
- UDP 使用每 session、每方向独立 AES-128-GCM key/salt、完整 16-byte tag、anti-replay、source pin 和 expiry。
- 未认证 UDP 包不得推进 sequence、绑定 source、进入 decoder 或产生放大响应。
- 所有 control/media 输入有硬长度上限；日志不输出凭据、key、完整 URL query、音频或无界 payload。

## 7. 证据状态

Canonical schema、fixtures 和 Python/C++ consumers 必须通过 contract/host gate。真实 Server、native firmware、
WSS/UDP HIL 和声学状态统一见 [Release readiness](../quality/release-readiness.md)。

详见：

- [RVA Control v1](rva-control-v1.md)
- [WSS Opus v2](wss-opus-v2.md)
- [Xiaozhi Control v1](xiaozhi-control-v1.md)
- [WSS Opus v1](wss-opus-v1.md)
- [UDP Opus GCM v1](udp-opus-gcm-v1.md)
- [生命周期与错误](lifecycle-errors.md)
