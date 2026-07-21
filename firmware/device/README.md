# Firmware headless contract harness

本目录是独立的 ESP-IDF headless 验证工程，不是 production firmware composition。正式应用入口位于
`firmware/apps/voice_terminal`，共享的 production components 位于 `firmware/components`。

该 harness 只链接：

- `voice_contracts`：transport profile、typed UDP header、datagram framing 与 nonce contract；
- `voice_core`：session、media owner、playback generation、cancel 与 close lifecycle。

它用于验证核心组件不依赖 board、audio、network、LVGL 或具体 transport runtime。`main/` 仅提供 ESP-IDF
compile/link smoke，不能作为设备固件烧录，也不能证明显示、触摸、网络、音频或声学能力。

验证：

```powershell
./scripts/verify-component-boundaries.ps1
./scripts/build-headless.ps1 -Clean
```

当前发布证据与未运行门禁统一见 [Release readiness](../../docs/quality/release-readiness.md)。
