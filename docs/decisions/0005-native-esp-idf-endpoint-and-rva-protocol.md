# 决策 0005：ESP32 迁移为项目自有 ESP-IDF endpoint 与 RVA 协议

日期：2026-07-21
状态：accepted / protocol and compatibility boundary superseded by [0006](0006-server-authoritative-interruption-and-rva-v2.md)
实施状态：native endpoint、RVA control 与中性 transport 已成为当前主线；下述“背景”描述决策作出时的旧状态，
不代表当前 production composition。精确发布证据以 [Release readiness](../quality/release-readiness.md) 为准。

## 背景

当前 production firmware 以固定 `xiaozhi-esp32` application 加 Product overlay 交付。该组合已经验证显示、
触摸、音频、AEC、WSS/UDP 和端云闭环，但仍链接 MCP、MQTT、Xiaozhi OTA/assets/activation 等当前产品不需要的
能力，并让 Product wire、构建和应用生命周期持续受上游 application 约束。

## 已考虑选项

- 继续扩大 Xiaozhi overlay：短期改动最少，但无关依赖、协议命名和 overlay 漂移会持续累积。
- 从空 ESP-IDF 工程一次性重写：最终边界清晰，但会同时改变板级、音频、协议、UI 和 Server，回归无法定位。
- 并行建立 native target 并按 parity gate 迁移：存在一段双实现期，但可以保留可用基线并逐层定位失败。

## 决定

- 新建项目自有 ESP-IDF native endpoint；`xiaozhi-esp32` target 在 parity 完成前保持可构建、可回滚。
- 底座使用 ESP-IDF 与经过锁定的 Espressif components；Product 自有 board、audio、session、transport、config 和 UI composition。
- 新增项目自有 `rva-control-v1`，不兼容 Xiaozhi `hello/listen/tts/abort/mcp` wire；应用握手改为
  `session.open/session.opened`。
- 媒体首版支持 `wss-opus-v2`，随后迁移已有 `udp-opus-gcm-v1`；设备每个 session 只 commit 一个 profile。
- 协议保留 session epoch、playback generation、精确 cancel、stale media fence、bounded queue、close reason、
  heartbeat/limits 和 fresh reconnect。
- Server 保留 legacy `/v1/xiaozhi` 作为迁移期 binding；新 `/v1/voice` binding 与 legacy binding 进入同一 canonical
  session/audio runtime，不把设备协议泄漏进 Agent/provider 层。
- MCP、MQTT、Xiaozhi activation、Xiaozhi OTA 和 remote assets 不进入 native target。未来 OTA 使用独立、签名验证的
  HTTPS 运维组件，不进入实时 session protocol。

## 后果与风险

源码、依赖和 wire ownership 将明显收敛，固件可在无 LVGL endpoint 中复用语音组件。代价是迁移期同时维护两条
firmware/binding，并需要重新验证立创板 I2S/TDM、AEC reference、队列时序、UI 和弱网行为。删除源码数量不能直接
推导 Flash、SRAM 或延迟收益，必须以 native artifact 实测。

## 兼容和迁移

迁移期间 Director/Worker 同时声明 legacy 与 RVA profiles；同一连接不做 wire 或 transport 热切换。只有 native
artifact 在同一板卡、网络和 provider 下达到受影响行为的同级证据，才归档 Xiaozhi target 和 Server binding。
浏览器/手机仍可新增标准 LiveKit Room binding，不要求复用 ESP32 task 或 transport 实现。

## 复查触发条件

- native WSS 无法在目标资源余量内达到当前音频/AEC/交互 parity。
- 官方 component 版本或许可证无法满足发布要求。
- 新 wire 的生命周期复杂度没有低于 legacy adapter，或出现无法稳定映射到 canonical runtime 的语义。
- 第二个 MCU/高性能 endpoint 对 profile/contract 提出已验证的冲突需求。

## 关联

- [生产固件 composition](0003-production-firmware-composition.md)
- [系统架构](../architecture/system.md)
- [固件架构](../architecture/firmware.md)
- [服务端打断单权威与 RVA v2](0006-server-authoritative-interruption-and-rva-v2.md)
