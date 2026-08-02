# realtime-voice-agent

面向嵌入式设备、浏览器和移动端的实时语音 Agent 端云工程。首个 endpoint 是立创实战派 ESP32-S3；
设备通过项目定义的 `rva/1` 与 `wss-opus/1` / `udp-opus-gcm/1` 接入 roomless LiveKit
`AgentSession`。服务端由 Session Director、可水平扩展的 Realtime Worker 和 provider adapters 组成。

默认产品路径是 native ESP-IDF endpoint、`/rva/v1/voice` 和 RVA wire。

## 当前能力

- Director 提供 Worker registry、capacity、route lease、fencing 和单次 connect grant。
- Worker 统一拥有 active session、Opus、Agent runtime、playback generation 和有界 teardown。
- Native ESP-IDF endpoint 将 board、audio/AFE、transport、config 和可选 LVGL UI 分离为独立组件。
- Python Desktop Reference Client 复用同一 RVA wire，提供确定性 headless E2E 和显式启用的本机声卡体验入口。
- Server 是语音打断的唯一裁决者；Endpoint 只执行带 `response_id + generation` 的播放 fence 并上报物理播放事实。
- `VOICE_WORKER_MAX_SESSIONS=5` 是可配置启动值，不代表容量 SLO。

发布状态和未运行项以 [Release readiness](docs/quality/release-readiness.md) 为准。任何 build、host test 或
历史 artifact 都不能单独替代当前源版本的真机、声学、弱网和长稳门禁。

## 目录

```text
protocol/       canonical control/media contracts and fixtures
server/         session_director, realtime_worker, shared contracts and tests
firmware/       native application and reusable endpoint components
clients/        protocol reference endpoints and deterministic host E2E clients
docs/           product, architecture, protocol, security, quality and operations
tests/          repository and cross-endpoint contract gates
third_party/    pinned upstream sources and license inventory
```

## 开发入口

根仓验证（Linux/macOS/WSL）：

```bash
uv sync --locked --dev
uv run ruff check scripts tests firmware/tools
uv run pytest
uv run python scripts/verify_repository.py
uv run python scripts/check_secrets.py
```

Server 运行入口只支持 Linux/container：

```bash
cd server
uv sync --locked --all-packages --dev
uv run session-director
uv run realtime-worker
```

多实例部署使用 [`deployment/single-node/`](deployment/single-node/) 的 Docker Compose 资产；测试中的独立
Director/Worker 子进程由 `voice_testkit.subprocess_cluster` 管理，不作为生产启动器。

Native firmware 入口为 `firmware/apps/voice_terminal/`。真实 provider、设备和网络凭据只写入 ignored
`.env` / local configuration；tracked 模板只保存字段和安全占位符。

文档从 [docs/index.md](docs/index.md) 开始阅读。Machine-readable `protocol/` 是 wire 的唯一权威。
