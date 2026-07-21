# realtime-voice-agent

面向嵌入式设备、浏览器和移动端的实时语音 Agent 端云工程。首个 endpoint 是立创实战派 ESP32-S3；
设备通过 Product 自有 `rva-control-v1` 与 `wss-opus-v2` / `udp-opus-gcm-v1` 接入 roomless LiveKit
`AgentSession`。服务端由 Session Director、可水平扩展的 Realtime Worker 和 provider adapters 组成。

## 当前能力

- Director 提供 Worker registry、capacity、route lease、fencing 和单次 connect grant。
- Worker 统一拥有 active session、Opus、Agent runtime、playback generation 和有界 teardown。
- Native ESP-IDF endpoint 将 board、audio/AFE、transport、config 和可选 LVGL UI 分离为独立组件。
- Legacy Xiaozhi binding/target 仅在 native parity 和兼容期结束前作为回滚线保留，不是新功能开发入口。
- `VOICE_WORKER_MAX_SESSIONS=5` 是可配置启动值，不代表容量 SLO。

发布状态和未运行项以 [Release readiness](docs/quality/release-readiness.md) 为准。任何 build、host test 或
历史 artifact 都不能单独替代当前源版本的真机、声学、弱网和长稳门禁。

## 目录

```text
protocol/       canonical control/media contracts and fixtures
server/         session_director, realtime_worker, shared contracts and tests
firmware/       native application, reusable components and compatibility target
docs/           product, architecture, protocol, security, quality and operations
tests/          repository and cross-endpoint contract gates
migration/      minimal immutable provenance for the compatibility baseline
third_party/    pinned upstream sources and license inventory
```

## 开发入口

```powershell
./scripts/bootstrap.ps1
./scripts/verify.ps1
./scripts/run-local.ps1
```

Native firmware 入口为 `firmware/apps/voice_terminal/`。真实 provider、设备和网络凭据只写入 ignored
`.env` / local configuration；tracked 模板只保存字段和安全占位符。

文档从 [docs/index.md](docs/index.md) 开始阅读。Machine-readable `protocol/` 是 wire 的唯一权威。
