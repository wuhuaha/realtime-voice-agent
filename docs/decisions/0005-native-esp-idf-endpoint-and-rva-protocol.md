# 决策 0005：项目自有 ESP-IDF endpoint 与 RVA 协议

日期：2026-08-01
状态：accepted；协议细节由 [决策 0006](0006-server-authoritative-interruption-and-rva-v2.md) 和 canonical
protocol/ contracts 约束。

## 决定

- 首个 reference endpoint 使用项目自有 ESP-IDF native application；入口为 `firmware/apps/voice_terminal/`。
- Board、audio、session、transport、config、presentation 和可选 LVGL UI 由 Product 自有组件组成；语音 core 不依赖
  board、LVGL 或具体 provider。
- 端云控制协议为 `rva-control-v2`，设备媒体 profile 为 `wss-opus-v3` 或 `udp-opus-gcm-v2`；同一 session 只提交一个
  profile，不做 transport 热切换。
- Endpoint 实现 session epoch、connect grant、playback generation、精确 stop、freshness fence、有界队列、heartbeat、
  close reason 和 fresh reconnect；Server 是语义打断和 response lifecycle 的唯一裁决者。
- 当前 Server 只提供 `/v2/voice`，旧 wire/path 不进入 Product runtime；未来其他 endpoint 通过 canonical contract
  和独立 binding 接入，不复制 ESP32 task 或 UI 实现。
- Server、firmware 与 protocol 必须作为匹配 artifact 发布；单边回滚到不兼容 wire 不受支持。

## 资源与验证边界

ESP-IDF 与 pinned components 提供底层工具链；native host contract、clean build/size、真机 boot、声学、弱网和长稳
分别记录证据等级。源码目录收口本身不等于 Flash、SRAM、延迟或稳定性收益，只有同源 artifact 的实际测量才能提升对应
release gate。

## 复查条件

- native endpoint 无法在目标资源余量内达到当前 WSS/UDP 交互和安全 contract。
- 新 endpoint 对 playback fact、cursor 或 profile 提出无法由 contract extension 表达的需求。
- pinned component 的许可证、ABI 或供应链约束不再满足发布要求。

## 关联

- [Firmware 架构](../architecture/firmware.md)
- [RVA Control v2](../protocol/rva-control-v2.md)
- [决策 0006：服务端独占语义打断](0006-server-authoritative-interruption-and-rva-v2.md)
