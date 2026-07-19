# Firmware target components

本目录是目标 component 架构的可编译起点，不是当前可发布固件。当前已验证功能仍由
`firmware/reference/xiaozhi-overlay/` 在固定 Xiaozhi upstream 上提供。

现有两个 component 有实际 contract 用途：

- `voice_contracts`：严格解析和输出 `wss-opus-v1`、`udp-opus-gcm-v1` profile 名称；
- `voice_core`：约束 fresh session generation、单 active media owner、profile/owner 匹配、
  playback generation 单调推进，以及 close 时立即撤销 owner。

二者均为纯 C++17，不依赖 ESP-IDF、board、audio、network 或 LVGL。`main/` 只用于 ESP-IDF
headless compile smoke，不做设备 bring-up。

验证：

```powershell
./scripts/verify-component-boundaries.ps1
./scripts/build-headless.ps1 -Clean
```

第一条是 host/source contract；第二条只证明 ESP-IDF `esp32s3` 编译链接和 image size，
不证明 reference 功能、开发板启动或外设可用。
