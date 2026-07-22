# Server 架构

状态：accepted target architecture
更新日期：2026-07-20

## 1. 设计原则

- Control plane 与 realtime data plane 分离。
- 一个 active session epoch 由一个 Worker 完整拥有。
- binding 只处理 wire、codec、transport health 和 lifecycle，不复制 Agent turn state。
- provider adapter 不反向依赖 transport。
- 所有热路径 queue、task、timeout 和 cleanup 有上限。
- 共享 store 只服务 registry、routing、grant 和 fencing，不进入媒体帧路径。

## 2. 目标目录

```text
server/
  apps/
    session_director/
      src/session_director/
        app.py
        config.py
        service.py
        store.py              # Port + memory/Redis adapters
      tests/
    realtime_worker/
      src/realtime_worker/
        app.py
        config.py
        auth.py
        bindings/rva/         # current /v1/voice control and session binding
        bindings/xiaozhi/     # isolated legacy compatibility facade
        transport/            # protocol-neutral media wire/gateway
        voice/livekit/        # roomless runner and audio/text I/O
        providers/
        resources/
        observability/
      tests/
  packages/
    voice_contracts/
    voice_testkit/
```

目录反映依赖方向而不是展示层级：RVA binding 是当前产品入口，legacy binding 只能依赖共享 runtime/transport，
共享实现不得从 legacy package 导入。拆分必须保持行为和测试，不得同时改变 wire、provider 和 runtime。

## 3. Director

### 3.1 API

- `POST /v1/session/bootstrap`：输入 tenant、device、supported profiles，返回 Worker URL、短期 grant、epoch、
  fencing token、allowed profiles 和 expiry。
- `POST /v1/session/release`：设备在本地建链失败或完整 teardown 后提交 tenant、device、Worker、epoch 和 fencing
  token；Director 使用精确 compare-and-delete 释放 route。请求使用与 bootstrap 相同且绑定 tenant/device 的认证，
  不存在、重复或旧 fence 均返回相同幂等成功响应，避免泄露当前 lease 状态。
- `POST /internal/v1/workers/heartbeat`：认证 Worker 上报 public URL、active/max、profiles、draining、healthy。
- `POST /internal/v1/workers/{worker_id}/drain`：单向进入 drain；v1 不允许对同一进程撤销 drain。
- `POST /internal/v1/grants/consume`：认证 Worker 请求 Director 原子消费 shared `jti` 并复核 route/fencing。
- `/health/live` 只证明进程存活；`/health/ready` 必须反映 shared store 是否可接受新 bootstrap。

具体 HTTP schema 以 `voice_contracts` 为准；新增字段必须向后兼容或提升 API 版本。

### 3.2 Worker 选择

候选 Worker 必须满足 heartbeat 未过期、`healthy=true`、`draining=false`、`active_sessions < max_sessions` 且
支持至少一个设备 profile。首版可采用确定性 least-loaded + stable tie-break；选择结果必须经 atomic lease/fencing
写入共享 store，不能依赖进程内先读后写。

### 3.3 Lease 与 fencing

- route key：`tenant_id:device_id`。
- 每次新 owner 产生新 `session_epoch` 和严格递增 `fencing_token`。
- grant 绑定 `worker_id/device_id/session_epoch/fencing_token/profiles/iat/exp/jti`。
- Worker 必须验证签名、audience、expiry、设备、Worker，并通过 Director 将 `jti` 在 shared coordination
  store 中原子单次消费；消费发生在 session admission，不能进入媒体热路径。
- store 不可用时不得签发无法 fencing 的新 grant。
- Redis adapter 的连接建立和单条命令必须分别有界；当前默认均为 1 秒，允许配置范围为 0.05 至 10 秒。
  Redis timeout 对外映射为不包含 URL、credential 或原始异常的 `503 coordination_unavailable`。

## 4. Realtime Worker

### 4.1 Process resources

Worker 启动时构造并复用：Settings、HTTP/TLS pools、VAD model、provider bulkheads、shared local admission、
UDP gateway、metrics/tracing 和 grant verifier。不得为每个 60 ms packet 新建线程池、client 或模型。

### 4.2 Session composition

一个 session owner 组合：

```text
Connection/Binding
  -> Session Supervisor mailbox
  -> selected Media Transport
  -> Opus codec and bounded queues
  -> roomless LiveKit AgentSession
  -> provider streams
  -> playback generation and close coordinator
```

外部 callback 只能投递有界事件，不得在 callback 内执行完整 teardown。Close 由 supervisor 幂等串行化，顺序为：
发布 revocation -> 停止新 ingress -> 递增/撤销 generation -> 取消 provider/Agent tasks -> 停止 codec/media tasks ->
关闭 transport -> 释放 admission。

### 4.3 Roomless LiveKit Agent

LiveKit `AgentSession` 是 VAD、STT、EOU、interruption、turn 和 response 的实时语义内核；它不加入 Room，
也不承担设备 transport。Worker 使用自定义 input/output adapter 把选中 profile 的 PCM 流映射到 AgentSession。

中文配置应具备：适合现场噪声的 VAD threshold、流式/2-pass FunASR、中文 system prompt、按 `。！？` 优先且
按 provider pause 的 `，；：` 辅助切分 TTS，以及禁止 Markdown 进入 TTS 的文本规范化。具体阈值必须通过
声学实验校准，不能从旧环境直接固化为生产结论。

### 4.4 Binding 与 transport

- `rva` binding：`session.open/opened`、typed WSS media、transcript/response、exact cancel 和 generation fence。
- `wss-opus-v2`：共享 32-byte media header、session/media identity、directional sequence 和 generation admission。
- `udp-opus-gcm-v1`：grant、socket/session map、AEAD、replay、source pin、reorder、expiry 和 stats。
- compatibility binding：旧 headers/JSON/WSS framing 的边界转换；不得拥有 provider、Agent turn 或共享 transport
  实现，也不得成为新 endpoint 的依赖。
- 所有 profile 对上暴露一致的 bounded audio/control port；不得把 WebSocket 或 `asyncio.DatagramTransport`
  泄漏到 Agent application。

## 5. 并发与背压

| 资源 | Owner | 满载/超时策略 |
| --- | --- | --- |
| process session slots | Worker admission | 拒绝新连接，WebSocket close `1013` |
| per-session control queue | Session supervisor | 限长；非法或持续拥塞关闭 session |
| uplink media queue | selected transport | 超时或丢弃过时帧，回到 live edge |
| downlink audio queue | playback owner | generation fence；不得跨 generation 堆积 |
| UDP datagram queue | UDP session | 静默丢弃并计数，禁止阻塞 event loop |
| provider semaphore | provider resource | fail-fast/timeout/circuit policy，不无界等待 |

默认 `VOICE_WORKER_MAX_SESSIONS=5` 是保守配置。生产值必须以目标机器相同 provider、codec、profile 和观测
开销下的 CPU、RSS、event-loop lag、queue pressure 与 p95/p99 压测决定。

TTS provider 的共享并发槽使用单独的排队 deadline；当前 `VOICE_TTS_QUEUE_TIMEOUT_SECONDS` 默认 0.25 秒，超时产生
retryable backpressure，不启动 provider 请求。MiMo SSE 入口按流式增量解析并强制限制：单行与单事件各 1 MiB、
每事件最多 256 条 `data:`、单个 base64 解码后音频 chunk 512 KiB、整次响应 8 MiB；越界视为不可重试的 provider
协议错误，避免异常响应造成无界内存增长。

## 6. Health、drain 与关闭

- `live`：event loop 和进程仍运行。
- `ready`：配置合法、必要 socket/resource 已建立、未 draining、provider readiness 通过；配置 Director 时还要求
  至少一次 coordination heartbeat 成功。
- `dependency health`：provider 和 Redis 单独报告，不与 liveness 混淆。
- `draining`：heartbeat 立即发布，Director 停止分配；Worker 拒绝新连接，现有 session 在 deadline 内收敛。
- Worker registry 完整关闭后再通过 draining heartbeat 上报 exact lease release；每个 heartbeat 合同最多携带 64 条，
  最多尝试 32 次，且全流程仍受 `VOICE_SHUTDOWN_DRAIN_TIMEOUT_SECONDS` 总预算约束（默认 10 秒）。
- deadline 或最大尝试次数到达：记录仍 pending 的 release，撤销 UDP key/grant 并退出；不得无限等待 provider 或
  coordination store。

## 7. 可观测性

统一字段：`tenant_id`、redacted `device_id`、`worker_id`、`session_id`、`session_epoch`、`profile`、`turn_id`、
`generation`、`provider`、`close_reason`。

必须测量：active/rejected sessions、bootstrap/handshake latency、ASR final、LLM first token、TTS first PCM、
device playout proxy、interrupt tail、queue depth/drop、UDP auth/replay/loss/late、event-loop lag、CPU、RSS、
provider error/429/timeout。日志不得替代 metrics，也不得记录 token、key、原始音频或 provider 原始错误体。

## 8. 实现状态

Director 默认选择 RVA binding，并可对显式兼容 client 选择 legacy binding；grant 绑定 Worker、device、session
epoch、fencing token、profiles、control protocol、expiry 和单次 `jti`。Worker 的 binding registry 共用 process
admission，并聚合 active lease、release、revoke 和 heartbeat，但新功能只进入 RVA 路径。

`/v1/voice`、RVA WSS binding/runtime、strict parser、Opus、bounded queues、transcript/response 和 exact cancel 已有
unit/contract evidence。真实 provider、部署和设备结论只在 [Release readiness](../quality/release-readiness.md) 更新。
