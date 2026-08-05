# 本地开发

更新日期：2026-07-30

## 1. 前置条件

- Linux、Git、Python 3.12、`uv`；Server 的本机运行和多进程测试不依赖 Windows 原生进程管理。
- Firmware 使用锁定 ESP-IDF 5.5.2。
- 多实例 coordination 使用 Redis-compatible service；memory backend 只用于单进程开发。
- 真实 provider 与设备凭据只写 ignored `.env` / local build input。

## 2. 初始化与验证

```bash
cp .env.example .env
uv sync --locked --dev
uv sync --directory server --locked --all-packages --dev
uv sync --directory clients/desktop_reference --locked --extra test
uv run ruff check scripts tests firmware/tools
uv run pytest
uv run python scripts/verify_repository.py
uv run python scripts/check_secrets.py
```

Windows PowerShell 脚本只用于固件/开发辅助，不是 Server 运行前提；在 Windows 上验证 Server 时请使用 WSL 或
Linux container。

Server 测试可单独运行：

```bash
uv run --directory server ruff check .
uv run --directory server pytest
```

Desktop Reference Client 可单独初始化和验证：

```bash
uv sync --directory clients/desktop_reference --locked --extra test
uv run --directory clients/desktop_reference ruff check src tests
uv run --directory clients/desktop_reference pytest -m "not e2e_host"
```

真实进程 E2E 会启动独立的 deterministic Director/Worker，不访问 provider；具体命令和环境变量见
[`clients/desktop_reference/README.md`](../../clients/desktop_reference/README.md)。

容量/churn 与 Linux netns/netem 专项不属于默认 `pytest`。命令、输出字段和证据限制分别见
[`server/README.md`](../../server/README.md) 与
[`clients/desktop_reference/README.md`](../../clients/desktop_reference/README.md)；未显式执行不得写为通过。

## 3. 启动 Server

```bash
cd server
uv run session-director
uv run realtime-worker
```

需要多 Worker、Redis 或固定网络端口时，使用 [`deployment/single-node/`](../../deployment/single-node/) 的 Docker Compose
资产。测试中的独立 Director/Worker 进程由 `voice_testkit.subprocess_cluster` 启动和回收，不能作为生产启动器。

检查 Director/Worker 的 `/health/live` 与 `/health/ready`。`live` 只证明进程存活；`ready` 还要检查配置、
coordination heartbeat 和启用的 provider/network policy。不得把 HTTP 200 单独当作语音闭环。

开发模式：

- `VOICE_RUNNER=deterministic`：验证协议、队列、lifecycle 和多 Worker，不访问 provider。
- `VOICE_RUNNER=livekit`：使用实际 FunASR/LLM/TTS；先单 Worker/WSS，再启用 UDP 或多实例。
- `VOICE_COORDINATION_BACKEND=memory`：单进程开发。
- `VOICE_COORDINATION_BACKEND=redis`：lease/fencing/grant 多实例验证。

## 4. Desktop 交互体验

桌面端与 ESP32 使用同一 `rva/1` 和 media profiles。真实麦克风/扬声器必须显式安装 interactive extras，
并从 ignored `.env` 或进程环境提供 Director URL、bootstrap token 和唯一 device ID。CLI 不接受或输出 provider key；
provider 仍只由 Worker 配置。首轮建议强制 WSS，确认 host 音频闭环后再显式选择 UDP。

Desktop 的 headless/interactive 都不是独立业务 Agent，也不是 Server 替代品。headless host 通过只能证明当前 host
协议链路；interactive 通过不能证明 ESP32 的 AEC、VAD、唤醒词、UI、内存或弱网行为。

## 5. Native firmware

入口：`firmware/apps/voice_terminal`。激活锁定 ESP-IDF 后：

```bash
cd firmware/apps/voice_terminal
idf.py set-target esp32s3
idf.py build
idf.py size
```

Windows 不要求也不推荐依赖当前 PowerShell 会话是否激活过 ESP-IDF。仓库根目录的
`scripts/build-firmware.ps1` 会校验 `third_party/sources.lock.yaml` 中的 ESP-IDF revision，固定已安装的
Python/CMake/Ninja/Xtensa 工具，并设置 `PYTHONUTF8=1`，避免 ESP-SR 模型脚本在中文 Windows 默认 GBK 下失败：

```powershell
pwsh -File .\scripts\build-firmware.ps1 -Clean
```

默认 build/config 均在 ignored 的 `firmware/apps/voice_terminal/build-local`；部署配置必须通过
`-Sdkconfig` 和独立 `-BuildDir` 显式传入。脚本不调用 `export.ps1`，因此缺少其他芯片的工具不会阻塞 ESP32-S3
构建。其他安装位置通过 `RVA_IDF_PATH`、`RVA_IDF_TOOLS_PATH` 指定。

Wi-Fi、Director URL 和 bootstrap token 使用 local Kconfig/build input，不进入 tracked defaults。配置改变后重新
configure/build，不能复用不匹配的 `sdkconfig`。烧录需要明确目标端口；记录 source identity、artifact digest、
board、IDF、命令和观察范围。

公开 bundle 的部署使用 bundle 内 `rva-device-provision.py`：依次 `validate`、五镜像 `flash`、仅 NVS 的
`provision`；需要恢复配置页时运行 `erase-config`。完整参数和 NVS 未加密边界见
[`firmware/release/FLASHING.md`](../../firmware/release/FLASHING.md)。

## 6. 最小开发顺序

1. 根 repository/protocol gate。
2. Server deterministic unit/contract。
3. Director + Worker + Desktop Reference Client 的 WSS/UDP deterministic host E2E。
4. 单 Worker真实 provider smoke。
5. Native clean build/size。
6. 授权后 WSS HIL。
7. UDP tamper/replay/weak-network 与 HIL。
8. Redis multi-worker、声学和长稳。

实际状态见 [Release readiness](../quality/release-readiness.md)。
