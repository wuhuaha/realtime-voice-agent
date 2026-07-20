# Firmware target components

本目录是 non-release component-extraction prototype，不是当前可发布固件。唯一 production composition 位于
`firmware/targets/lichuang-dev/`，由固定 Xiaozhi upstream 与仓内 overlay 组成。

现有两个 component 有实际 contract 用途：

- `voice_contracts`：严格解析 profile 名称，并提供 `udp-opus-gcm-v1` 的 typed v1 header、
  datagram framing 与 nonce contract；
- `voice_core`：约束 fresh session generation、单 active media owner、profile/owner 匹配、
  playback generation 单调推进，以及 generation-bound callback/send 和 close 时立即撤销 owner。

`TransportPort`、`AudioPort` 与 `EventSink` 只定义 session orchestration 所需的最小 port；当前没有
concrete audio 或 transport。二者均为纯 C++17，不依赖 ESP-IDF、board、audio runtime、network 或
LVGL。`main/` 只用于 ESP-IDF headless compile smoke，不做设备 bring-up。

验证：

```powershell
./scripts/verify-component-boundaries.ps1
./scripts/build-headless.ps1 -Clean
```

第一条是 host/source contract；第二条只证明 ESP-IDF `esp32s3` 编译链接和 image size，
不证明 production target 功能、开发板启动或外设可用。仓级状态见
[../MIGRATION_STATUS.md](../MIGRATION_STATUS.md)。
