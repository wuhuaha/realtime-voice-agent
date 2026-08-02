# 决策 0007：Python Desktop Reference Client

日期：2026-07-30
状态：accepted

## 决定

Product 维护独立的 `rva-desktop` Python reference endpoint，用于验证 canonical control/media contract、Director
bootstrap、`wss-opus/1` / `udp-opus-gcm/1`、playback facts 和 host lifecycle。它与 ESP32 共用协议和服务端边界，
但不成为最终用户桌面产品。

## 范围

- `headless` 与 `interactive` 共用 session、control、media、generation、freshness 和 close lifecycle。
- 音频设备通过 `AudioSource`/`AudioSink` ports 接入；fixture/null/recording backend 不依赖真实声卡。
- CI 默认使用固定 PCM/Opus 与 deterministic runner，分别覆盖 WSS 和 UDP；真实 provider、声卡、AEC 和公网连接
  通过显式 smoke/实验运行。
- CLI 和结构化 trace 是当前交付面；GUI、安装器、自动更新、签名分发、SBOM 和 native audio license 仍需独立门禁。
- Desktop client 不注册额外 server route，不实现旧 wire，不复制 ESP32 UI、task 或 provider 逻辑。

## 后果与风险

独立 reference endpoint 可以把 endpoint、Server 和 provider 故障分开定位，并提供不依赖开发板的确定性 E2E oracle。
代价是需要维护 Python transport、Opus 和可选 audio backend；阻塞声卡调用、native ABI、codec 构建选项和跨平台
许可证必须由发布前检查覆盖。host E2E 不证明 ESP32 的 AEC、VAD、UI、内存、弱网、声学或长稳行为。

## 复查条件

- 第二个 Python endpoint 需要共享 transport/session core。
- 浏览器或移动端需要标准 RTC binding。
- interactive backend 的维护或许可证成本超过 reference 验证收益。

## 关联

- [系统架构](../architecture/system.md)
- [测试策略](../quality/test-strategy.md)
- [RVA Protocol 1.0](../protocol/rva-protocol-v1.md)
