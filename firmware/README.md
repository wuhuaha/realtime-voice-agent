# Firmware

## 正式结构

- `apps/voice_terminal/`：Product 自有 ESP-IDF application composition。
- `components/`：board、audio/AFE、RVA protocol、WSS/UDP transport、configuration、runtime 和可选 LVGL UI。
- `device/`：独立 headless contract harness，仅消费 `components/voice_contracts` 与 `components/voice_core`，
  不提供 production runtime source，也不生成发布镜像。

核心语音与 transport 不依赖 LVGL。板级引脚、codec、I2S/TDM、显示和触摸事实集中在
`board_lichuang_s3`；应用只组合组件并拥有顶层生命周期。

当前构建、真机和未运行门禁见 [Release readiness](../docs/quality/release-readiness.md)。

开发、CI 和发布候选只从 `apps/voice_terminal/` 构建。
