# 决策：Python Desktop Reference Client

日期：2026-07-30
状态：accepted
决定：在 Product 仓维护独立的 Python RVA reference endpoint，同一协议核心组合 deterministic headless E2E
与 interactive desktop 体验；PyXiaozhi 只作为音频、AEC、UI 和打包实现来源，不引入其 wire 或无关功能。

## 背景

当前 Product 已有 native ESP32 endpoint、Director、Realtime Worker、canonical RVA v2 wire 和分层测试策略，
但缺少不依赖开发板的完整 reference endpoint。仅用真机定位问题会把 firmware、网络、provider 和服务端故障混在
同一观察面；仅用 server 内部 fake 又不能证明外部 endpoint 的 bootstrap、wire 和 playback lifecycle。

PyXiaozhi 提供成熟的跨平台 microphone/speaker、Opus、重采样、AEC、GUI/CLI 与打包经验，但其会话、WebSocket、
MQTT/UDP、帧参数和安全默认值均不等于 RVA v2。

## 已考虑选项

- 整仓 fork PyXiaozhi：最早获得 UI，但会继承 Xiaozhi wire、MCP/OTA/activation/IoT、重依赖和上游重构成本。
- 在 PyXiaozhi 内增加 RVA adapter：可保留更多现成交互，但两套 lifecycle 长期共存，E2E oracle 仍受应用状态影响。
- 独立实现 RVA reference endpoint并选择性提炼音频能力：初始实现量较高，但 wire、测试和资源 ownership 可控。
- 只做无头协议脚本：测试成本最低，但不能提供电脑端声学体验和设备诊断。

## 证据

- `protocol/rva_control_v2/contract.yaml`、`wss-opus-v3` 和 `udp-opus-gcm-v2` 定义 Product current wire。
- `docs/quality/test-strategy.md` 已要求独立进程 Director、Worker、Redis 和 reference client。
- 固定 PyXiaozhi `v2.1.1`/`95bd792` 的源码核对表明其音频能力可参考，但协议和音频帧参数不兼容 RVA。

## 决定与范围

- Product 新增 `clients/desktop_reference`，包名为 `rva-desktop`。
- `headless` 与 `interactive` 共用 Director、session、control、media、generation 和 freshness 核心。
- 音频设备通过 `AudioSource`/`AudioSink` ports 接入；fixture/null/recording backend 不依赖真实声卡。
- 默认 CI 只使用固定 PCM/Opus 和 deterministic runner；真实 provider、麦克风、扬声器和 AEC 是显式 smoke/实验。
- 首版提供 CLI 和结构化 trace；完整桌面 GUI 只有在 CLI/host E2E 稳定后再评估。
- 不提供 Xiaozhi wire compatibility，不注册 legacy server route，不复制 PyXiaozhi native binary。

## 后果与风险

正面结果：可独立定位 endpoint/server/provider 故障；WSS/UDP 获得同一外部 oracle；电脑端可录音、回放和观察
逐事件 lifecycle；未来浏览器/手机 endpoint可复用测试方法。

代价：需要维护 Python transport、Opus 和 audio backend；跨平台声卡、AEC 和打包必须分别验证；协议变更需要同步
Firmware、Server 和 Desktop contract tests。

风险：真实音频 backend可能阻塞 asyncio；native codec/APM 有供应链和 ABI 风险；interactive 结果不可代替
deterministic E2E。通过 optional dependencies、显式线程/queue owner、固定 fixtures 和许可证门禁控制。

## 兼容和迁移

该 endpoint 是新消费者，不改变现有 ESP32、Director、Worker 或 wire。任何接入所需的 wire 修改都必须先更新
canonical protocol 和 ADR，不能在 client 私有扩展。PyXiaozhi 只读 checkout保留在 Research ignored external区域。

## 复查触发条件

- 第二个 Python endpoint 需要复用 session/transport core。
- 浏览器或手机需要标准 RTC binding。
- 跨平台 AEC/native codec 的许可证、ABI 或维护成本超过独立实现收益。
- Headless 与 interactive 无法共用 lifecycle 而出现协议漂移。

## 关联链接

- [系统架构](../architecture/system.md)
- [测试策略](../quality/test-strategy.md)
- [RVA Control v2](../protocol/rva-control-v2.md)
- Research：`docs/research/2026-07-30-py-xiaozhi-desktop-reference-client.md`
