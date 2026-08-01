# 系统架构

状态：accepted target architecture
更新日期：2026-07-30

## 1. 架构目标

系统以稳定控制面和 stateful 实时 Worker 分离实现水平扩展，同时保持一对一媒体单跳。ESP32-S3 是首个
endpoint；未来浏览器、手机或其他 MCU 应通过新的 binding/profile 接入，而不是修改 Agent 核心。

```mermaid
flowchart LR
    E["ESP32 / Desktop reference / future endpoints"] -->|"HTTPS bootstrap"| D["Session Director"]
    D <--> R["Redis coordination store"]
    W1["Realtime Worker A"] -->|"heartbeat / drain"| D
    W2["Realtime Worker B"] -->|"heartbeat / drain"| D
    E -->|"WSS control + selected media"| W1
    W1 --> A["roomless LiveKit AgentSession"]
    A --> P["ASR / LLM / TTS providers"]
```

## 2. 部署单元与职责

### Session Director

- 提供设备 bootstrap 稳定入口。
- 维护 Worker registry、heartbeat TTL、capacity、health 和 drain 状态。
- 为 `tenant_id + device_id` 分配 route lease、递增 fencing token 和 session epoch。
- 签发绑定 Worker、设备、epoch、profiles、expiry 和 `jti` 的短期 connect grant。
- 执行全局 admission；不创建 AgentSession，不读取或转发媒体帧。

### Realtime Worker

- 终止 `/v2/voice` RVA WSS control；active runtime 只注册 canonical v2 route，其他 route fail closed。
- commit `wss-opus-v3` 或 `udp-opus-gcm-v2`，并持有对应媒体 transport。
- 一个 active session 内唯一持有 AgentSession、codec、provider stream、playback generation 和 teardown。
- 通过 `InterruptionCoordinator` 独占语音打断裁决；Endpoint VAD 只产生观测事实，不改变播放状态。
- 执行本地 `max_sessions` admission，默认 `5`，可按部署配置覆盖。
- 上报 heartbeat、active sessions、profiles、health 和 draining。

### Coordination Store

- 生产使用 Redis-compatible shared store，保存带 TTL 的 Worker registry、route lease、fencing 和 grant 单次消费状态。
- `memory` backend 只允许单进程开发和确定性测试，不能证明多实例安全。
- store 不进入逐帧媒体、VAD、ASR、TTS 或 playback 路径。

### Provider

- ASR、LLM、TTS 通过 Worker 内 adapter 接入。
- 连接池、模型资源和 concurrency bulkhead 尽量进程级复用；stream、请求、取消和 conversation 保持 session 级隔离。
- Provider 失败映射为有界 close/retry policy，不允许旧 generation 输出恢复进入播放。

### Desktop Reference Endpoint

- `clients/desktop_reference` 是 Product 内的 Python RVA reference endpoint，不是第二套协议实现来源。
- `headless` 使用固定 PCM source 与 recording/null sink，验证 Director bootstrap、WSS/UDP、control、media、
  generation/fence、fresh reopen 和 playback facts，不访问真实声卡或 provider。
- `interactive` 在同一 session/transport 核心上组合 `sounddevice` 麦克风与扬声器，只作为显式 host smoke 和桌面体验。
- interactive playback fact 使用 PortAudio clock 与报告的 output latency 形成 host 预计 render boundary；该边界不等于
  已测量的 DAC/扬声器输出，也不能替代声学验证。
- PyAV/libopus 与 PortAudio 是可选 host 依赖；ESP32、Server 和默认无声卡测试不依赖它们。
- Desktop endpoint 只实现 canonical RVA wire，不引入 MQTT、MCP、OTA、activation、IoT 或视觉业务依赖。

## 3. 核心不变量

1. 一个 `tenant_id/device_id/session_epoch` 最多一个 active Worker owner。
2. Director 不接触媒体、Agent turn、UDP key 明文使用过程或 provider stream。
3. WSS control 与 UDP media 必须落到同一个 Worker。
4. 一个 session 只 commit 一个 media profile；进行中不热切换。
5. 连接或 lease 失效后 fresh bootstrap，旧 epoch、grant、key 和 generation 全部失效。
6. 媒体热路径只访问进程内有界结构，不同步访问 Redis、数据库或消息队列。
7. `protocol/` 是 wire contract 唯一 authoring source。

## 4. 数据流

### Bootstrap 与路由

```mermaid
sequenceDiagram
    participant E as ESP32/Desktop Endpoint
    participant D as Director
    participant S as Shared Store
    participant W as Worker
    E->>D: bootstrap(device_id, supported_profiles)
    D->>S: read eligible workers and route lease
    D->>S: fenced lease + single-use grant metadata
    D-->>E: worker_wss_url + connect_grant + epoch
    E->>W: WSS /v2/voice + grant
    W->>D: consume grant through internal API
    D->>S: atomically consume jti + validate route/fence
    W-->>E: session.opened + committed profile
```

### WSS profile

WSS 同时承载 JSON control 和 binary Opus。WebSocket 的有序可靠语义简化接入，但 TCP loss 可能造成 HOL；
因此队列、message size、media age 和 stale generation 必须有界。

### UDP profile

WSS 继续承载控制、鉴权、grant 和 lifecycle；UDP 只承载认证后的 Opus datagram。authenticated probe 绑定
source，固定小型 reorder/jitter 等待处理轻度乱序，超时丢弃并使用 PLC。WSS 断开立即撤销 UDP session。

### Agent turn

Endpoint audio -> codec decode -> AgentSession input -> streaming ASR -> LLM delta -> 中文 TTS chunking -> streaming
PCM/Opus -> selected transport -> endpoint playout。播放期间音频仍进入同一 STT；服务端按声学候选、ASR 文本和
strict explicit policy 裁决打断。裁决成立后先发布 generation fence，再异步停止 Agent/provider；旧队列和旧网络包
不得恢复。

## 5. 信任边界

| 边界 | 不可信输入 | 必须控制 |
| --- | --- | --- |
| Internet -> Director | device identity、bootstrap payload | TLS、schema、rate limit、quota、审计 |
| Endpoint -> Worker WSS | header、JSON、binary frame | grant、origin/size/state/session 校验、bounded queue |
| Internet -> Worker UDP | 任意 datagram/source | framing、AEAD、anti-replay、source pin、expiry、rate limit |
| Worker -> Provider | 用户音频和文本 | timeout、concurrency、data policy、错误脱敏 |
| Operator -> Config/Store | secret、capacity、endpoint | 最小权限、secret manager、rotation、审计 |

## 6. Failure domain

- Director 短时不可用：已建立且 lease 安全窗口内的 session 继续；新 bootstrap 失败。
- Redis 不可用：Director fail closed，不产生无 fencing 的新 owner；既有媒体不访问 Redis。
- Worker crash：该 Worker 上 session 终止，设备 fresh bootstrap；不迁移 active turn。
- Provider 超时/429/5xx：隔离在 Worker/provider bulkhead，按可重试分类关闭或重试请求，不重放副作用。
- UDP blocked：`auto` 当前保守使用 WSS；显式 UDP 测试失败则关闭 fresh session，不在同 session 偷切 WSS。
- Wi-Fi 切换/WSS 断开：撤销 UDP key，清空播放 generation，重新建立 session。

## 7. 当前实现与目标差距

Server 实现 Director、memory/Redis store、单一 RVA v2 binding、shared admission/lease、Realtime Worker、roomless
Agent runtime 和 providers。当前验证等级与未运行项只在 Release readiness 记录。

Native ESP-IDF endpoint 已完成 board/audio/config/WSS/UI/UDP 组件化实现，composition 的完整性由 build、
host test 与 HIL 分层验证。当前实现、构建和发布差距只在
[Release readiness](../quality/release-readiness.md) 维护，架构文档不记录临时环境或 artifact 编年史。

Python Desktop Reference Client 已提供与 native endpoint 相同的 RVA v2 session/transport 语义。它用于把协议与
服务端问题从板级资源、声学环境和 UI 中隔离出来；host 通过不能替代 ESP32 HIL 或 acoustic verification。

## 8. 演进边界

- 浏览器/手机出现真实需求时，可新增标准 LiveKit Room binding；不把 Room/SFU 引入 ESP32 profile。
- Worker endpoint 无法在目标网络可靠暴露时，重新评估 Edge Media Gateway；在此之前不预建媒体转发层。
- 第二个真实 Agent application 消费者出现后再提取业务 Agent package。
- 标准 RTC/SFU、PCM DataChannel 和其他未登记媒体协议不属于当前产品范围。
