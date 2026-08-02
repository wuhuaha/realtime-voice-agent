# 实时语音 Agent 产品需求文档

版本：1.0
状态：accepted
更新日期：2026-07-23

## 1. 产品定义

`realtime-voice-agent` 是面向低资源嵌入式设备、浏览器和移动端的实时语音 Agent 端云工程。首个交付
endpoint 是立创实战派 ESP32-S3，但协议、服务端和测试体系不得永久绑定该硬件。

首版以通用中文闲聊为评测载荷，目标不是业务知识正确性，而是证明端云语音链路、实时交互、打断、
字幕、配置、故障恢复和水平扩展边界。Server 使用 roomless LiveKit `AgentSession` 组织 VAD、STT、LLM、
TTS 和 interruption；ESP32 使用 `rva/1`，媒体可选择 `wss-opus/1` 或
`udp-opus-gcm/1`。

## 2. 用户与场景

| 角色 | 主要目标 |
| --- | --- |
| 终端用户 | 通过触摸或唤醒进入中文语音对话，看到实时状态和字幕，并能自然打断播放 |
| 评测人员 | 在同一设备上 A/B 比较 WSS 与 UDP，记录延迟、稳定性、弱网和声学结果 |
| 开发人员 | 从本仓独立复现 Server、协议和固件构建，定位端云问题 |
| 运维人员 | 部署 Director 与多个 Worker，管理容量、drain、凭据、provider 和故障恢复 |

## 3. 产品范围

### 3.1 首版范围

- ESP32-S3 启动、联网、音频采集/播放、AEC 配置、触摸/唤醒、状态和流式字幕。
- 未配置网络时的 Wi-Fi 扫描、选择、软键盘输入和持久化；LiveKit Agent 服务 endpoint 配置和持久化。
- `rva/1` 控制协议与 `/rva/v1/voice` Worker endpoint。
- 始终可用的 `wss-opus/1` baseline。
- 显式选择或灰度使用的 `udp-opus-gcm/1` challenger；WSS 始终保留控制连接。
- Session Director、可水平扩展 Realtime Worker、共享 coordination store 适配和 worker-bound grant。
- roomless LiveKit Agent，支持流式 FunASR、DeepSeek-compatible LLM 和可配置流式 TTS provider。
- 中文 VAD/EOU、流式字幕、提前准备回复、自然打断和 playback generation fence。
- 本地开发、部署、安全、兼容、测试和故障排查文档。

### 3.2 非目标

- 不提供标准 RTC/SFU、PCM DataChannel 或其他未登记的媒体协议。
- 不为 ESP32 补齐 ICE、SDP、DTLS-SRTP、RTCP、SFU 或完整 WebRTC。
- 不在首版实现 active turn 跨 Worker 热迁移、same-session transport 切换或断点续播。
- 不把浏览器/手机标准 LiveKit Room 作为首个验收 endpoint；架构只保留未来接入边界。
- 不承诺业务 tools、RAG、长期记忆、多租户计费或内容安全策略的产品完成度。
- 不把默认 `max_sessions=5` 解释为容量测量、并发 SLO 或目标机器能力。

## 4. 功能需求

| ID | 需求 | 验收要点 |
| --- | --- | --- |
| FR-001 | 设备配置 | 无有效 Wi-Fi 时可扫描、选择、输入并保存网络；可配置并保存服务 endpoint |
| FR-002 | 会话 bootstrap | 设备可获得唯一 Worker endpoint、短期 connect grant、epoch、fencing token 和允许 profile |
| FR-003 | 身份与鉴权 | Director/Worker 拒绝缺失、过期、错设备、错 Worker、重放或篡改的 grant |
| FR-004 | 控制连接 | 客户端通过 `/rva/v1/voice` 完成 session、transcript、response、server stop fence、playback facts 和 close 流程 |
| FR-005 | WSS 媒体 | WSS 二进制帧双向承载完整 Opus packet，控制与媒体队列均有界 |
| FR-006 | UDP 媒体 | WSS 协商短期 UDP grant，UDP 通过 AES-128-GCM、anti-replay、source pin 和小型 jitter 传输 Opus |
| FR-007 | Profile 选择 | 设备可在 UI 显式选择 WSS/UDP；`auto` 必须遵守 server policy 和设备 capability |
| FR-008 | 中文语音链路 | 支持流式 ASR、LLM 增量响应、流式 TTS 和中文文本规范化 |
| FR-009 | 状态与字幕 | 设备展示连接、聆听、思考、播放、错误状态以及流式 ASR/TTS 文本 |
| FR-010 | 触发与打断 | 触摸或唤醒可开始交互；服务端独占语音打断裁决，端侧只执行精确 stop fence；显式按钮可发起 cancel request |
| FR-011 | Fresh session | WSS 断开、UDP 失活、lease/fencing 失败或网络切换后关闭旧 session 并重新 bootstrap |
| FR-012 | Worker admission | Worker 根据可配置 `max_sessions` 拒绝超额新会话；默认值为 `5` |
| FR-013 | Worker drain | Director 可将 Worker 标记 draining；不再分配新会话，现有会话有界收敛 |
| FR-014 | Provider 可替换 | ASR/LLM/TTS 通过明确 adapter 边界配置，transport 不依赖厂商 wire |
| FR-015 | 可观测关联 | 日志和指标可按 `worker_id/device_id/session_id/epoch/generation/profile` 关联且不泄密 |

## 5. 非功能需求

| ID | 领域 | 要求 |
| --- | --- | --- |
| NFR-001 | 实时性 | 记录 speech end、ASR final、LLM first token、TTS first audio、device first playout 和 interruption tail；门限在同环境 baseline 后冻结 |
| NFR-002 | 有界资源 | control/media/provider queue 必须声明容量、超时、满载策略和关闭语义；过载优先丢弃过时媒体或拒绝新会话 |
| NFR-003 | Session ownership | 一个 session epoch 只有一个 Worker、一个媒体 owner、一个 AgentSession 和一个 generation owner |
| NFR-004 | 水平扩展 | Director 可多实例使用共享 store；Worker 可横向增加；媒体热路径不得经过 Redis 或数据库 |
| NFR-005 | 安全 | WSS 使用 TLS；UDP 使用每 session 双向独立 AEAD key/salt；所有输入限长、校验并 fail closed |
| NFR-006 | 隐私 | 默认不保存原始音频、provider 原始错误体或完整转写；诊断采集必须显式开启并有保留期限 |
| NFR-007 | 可维护性 | `protocol/` 是 wire 唯一 authoring source；固件核心语音不得依赖 LVGL；Provider 不泄漏到 binding |
| NFR-008 | 可复现性 | 固定外部 revision、依赖 lock、配置形状和构建工具链；真实 secret 与生成制品不得提交 |
| NFR-009 | 兼容性 | wire/lifecycle 变更必须运行 reference/new 四象限，WSS 与 UDP 分别验证 |
| NFR-010 | 可运维性 | liveness、readiness、draining、overload 和 dependency health 分开表达；支持 rolling drain 和回滚 |
| NFR-011 | 性能证据 | p95/p99、CPU、RSS、event-loop lag、queue pressure、丢包和 media age 只使用真实测量，不从默认值推断 |

## 6. 核心用户流程

### 6.1 首次联网

1. 设备读取 NVS；无有效网络时进入 Wi-Fi 配置页。
2. 用户扫描并选择 SSID，输入密码，配置服务 endpoint。
3. 设备保存配置并重连；日志不得打印密码、token 或完整含凭据 URL。
4. 配置无效时留在可恢复 UI，不得无限重启。

### 6.2 建立语音会话

1. 设备向 Director bootstrap；开发模式可使用明确标记的直连 Worker 路径。
2. Director 选择 non-draining 且有容量、支持所需 profile 的 Worker，签发短期 grant。
3. 设备连接 Worker `/rva/v1/voice`，发送 `session.open`。
4. Worker 返回 `session.opened` 并 commit 一个媒体 profile。WSS 立即可用；UDP 需完成 authenticated probe 后才 active。
5. 设备开始上行音频，Worker 启动 roomless AgentSession。

### 6.3 对话与打断

1. 用户触摸或唤醒后进入 listening，并持续上传当前 session 的音频。
2. 端侧持续发送 Opus；服务端发回 ASR partial/final。
3. LLM 增量文本按中文标点切分并提前触发 TTS。
4. 服务端发送 `response.begin/text/end` 和音频；端侧有界预缓冲后播放。
5. 用户近讲音频继续进入服务端 STT；只有 strict explicit policy 命中时服务端发送精确 `playback.stop`。点击停止则
   发送 `response.cancel.request`，由服务端确认并发布同一 fence。Endpoint 不用 VAD 自行清队列。
6. Endpoint 在首个样本进入 DAC 时发送 `playback.started`，在真实播放完成、停止或失败时发送 `playback.ended`。

## 7. 验收与发布门禁

| Gate | 通过条件 | 证据入口 |
| --- | --- | --- |
| A. Repository | secret/license/source lock/目录依赖检查通过 | `docs/quality/release-readiness.md` |
| B. Server | unit、contract、integration、host e2e 通过；Director/Worker/Redis 语义可复现 | `docs/quality/release-readiness.md` |
| C. Firmware | native clean build、size、artifact identity 可复现 | `docs/quality/release-readiness.md` |
| D. WSS HIL | 当前 native firmware 与 Server 完成真实 provider、字幕、打断和播放闭环 | `docs/quality/release-readiness.md` |
| E. UDP HIL | grant/probe/GCM、上下行音频、generation、重连完成真机闭环 | `docs/quality/release-readiness.md` |
| F. 声学 | 近讲、远讲、播放中 double-talk 和回声抑制按固定方法评测 | `docs/quality/release-readiness.md` |
| G. 稳定性 | 至少 20 轮对话和 30 分钟会话，无 panic/WDT/失控自问自答/持续队列增长 | `docs/quality/release-readiness.md` |
| H. 弱网 | WSS/UDP 在同一 loss/burst/jitter 模型下 A/B，保留原始指标 | `docs/quality/release-readiness.md` |
| I. Scale | Redis backend 下多 Director/Worker 的 lease/fencing/drain/overload 验证 | `docs/quality/release-readiness.md` |

Compatibility baseline 只用于回归对照，不能替代 native artifact 的发布验收。当前精确状态统一见
[Release readiness](../quality/release-readiness.md)。

## 8. 成功判定

首版可发布必须同时满足：仓库可独立构建和部署；需求可追溯；WSS baseline 真机通过；UDP challenger
通过显式测试或被配置为不可默认选择；所有未通过项有阻塞、owner 和 waiver。不得用目录创建、host test、
旧日志或 `build_passed` 代替 `device_verified`、`acoustic_verified` 或 `measured`。

关联文档：

- [系统架构](../architecture/system.md)
- [协议总览](../protocol/overview.md)
- [需求追踪](../quality/requirements-traceability.md)
