# Firmware

## 正式结构

- `apps/voice_terminal/`：Product 自有 ESP-IDF application composition。
- `components/`：board、audio/AFE、RVA protocol、WSS/UDP transport、configuration、runtime 和可选 LVGL UI。
- `targets/lichuang-dev/`：隔离的 compatibility/rollback target；完成 parity 和支持期门禁前保持可构建，
  不进入 native 依赖链。
- `locks/`：compatibility target 所需的受控 dependency lock。
- `device/`：独立 headless contract harness，仅消费 `components/voice_contracts` 与 `components/voice_core`，
  不提供 production runtime source，也不生成发布镜像。

核心语音与 transport 不依赖 LVGL。板级引脚、codec、I2S/TDM、显示和触摸事实集中在
`board_lichuang_s3`；应用只组合组件并拥有顶层生命周期。

当前构建、真机和未运行门禁见 [Release readiness](../docs/quality/release-readiness.md)。

默认开发、CI 和发布候选只从 `apps/voice_terminal/` 构建。任何使用 compatibility target 的操作必须显式进入
其目录，并且其结果不能替代 native artifact 的发布证据。
