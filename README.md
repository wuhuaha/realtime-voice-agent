# realtime-voice-agent

[![CI](https://github.com/wuhuaha/realtime-voice-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/wuhuaha/realtime-voice-agent/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/wuhuaha/realtime-voice-agent?include_prereleases)](https://github.com/wuhuaha/realtime-voice-agent/releases)
[![License](https://img.shields.io/github/license/wuhuaha/realtime-voice-agent)](LICENSE)

面向低资源设备的实时语音 Agent 端云接入工程。当前 reference endpoints 是立创实战派 ESP32-S3 和 Python
Desktop Reference Client；协议边界不绑定特定设备类型，浏览器、移动端和其他 MCU 可按同一 wire 适配。Endpoint
通过项目定义的 `rva/1` 与 `wss-opus/1` / `udp-opus-gcm/1` 接入 roomless LiveKit `AgentSession`。服务端由
Session Director、可水平扩展的 Realtime Worker 和 provider adapters 组成。

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

`v0.1.0-alpha.1` 是技术预览，不是 production-ready 声明。发布边界见
[Known limitations](docs/quality/known-limitations.md)，版本内容见
[Release notes](docs/quality/release-notes-v0.1.0-alpha.1.md)。

## 五分钟验证

在 Linux、macOS 或 WSL 中安装 Python 3.12、Git 和 [`uv`](https://docs.astral.sh/uv/)，然后从仓库根目录执行：

```bash
uv sync --directory server --locked --all-packages --dev
uv sync --directory clients/desktop_reference --locked --extra test
uv run --directory clients/desktop_reference pytest \
  tests/e2e/test_deterministic_host.py -m e2e_host -vv
```

该命令不需要 API key、Redis、声卡或开发板，会启动独立 Director/Worker，分别完成 WSS/UDP 的 bootstrap、Opus、
control/media、playback facts 和资源回收。它验证协议与进程闭环，不代表真实 provider、硬件或公网质量。

真实服务、Desktop 音频体验和 ESP32-S3 发布固件的完整入口见 [Getting started](docs/getting-started.md)。

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

Server 运行入口只支持 Linux/container。配置文件必须放在启动进程的工作目录或导出为环境变量；不要直接使用模板中的
占位符：

```bash
cp .env.example .env
# 编辑 .env 后：
set -a; source .env; set +a
uv sync --directory server --locked --all-packages --dev
uv run --directory server session-director
# 在另一个已加载同一 .env 的终端运行：
uv run --directory server realtime-worker
```

多实例部署使用 [`deployment/single-node/`](deployment/single-node/) 的 Docker Compose 资产；测试中的独立
Director/Worker 子进程由 `voice_testkit.subprocess_cluster` 管理，不作为生产启动器。

Native firmware 入口为 `firmware/apps/voice_terminal/`。真实 provider、设备和网络凭据只写入 ignored
`.env` / local configuration；tracked 模板只保存字段和安全占位符。

文档从 [docs/index.md](docs/index.md) 开始阅读。问题反馈见 [Contributing](CONTRIBUTING.md)，使用支持边界见
[Support](SUPPORT.md)，安全问题请遵循 [Security policy](SECURITY.md)。Machine-readable `protocol/` 是 wire 的唯一权威。

发布时可从锁文件生成不带时间戳的 CycloneDX 1.5 SBOM：

```bash
uv run python scripts/generate_release_sbom.py --output artifacts/release-sbom.cdx.json
uv run python scripts/generate_release_sbom.py --output artifacts/release-sbom.cdx.json --check
```

公开 ESP32-S3 bundle 的 clean build、provenance 和打包命令见
[`firmware/apps/voice_terminal/README.md`](firmware/apps/voice_terminal/README.md#构建)。
