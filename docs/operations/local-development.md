# 本地开发

更新日期：2026-07-30

## 1. 前置条件

- Windows PowerShell 7、Git、Python 3.12、`uv`。
- Firmware 使用锁定 ESP-IDF 5.5.2。
- 多实例 coordination 使用 Redis-compatible service；memory backend 只用于单进程开发。
- 真实 provider 与设备凭据只写 ignored `.env` / local build input。

## 2. 初始化与验证

```powershell
Copy-Item .env.example .env
./scripts/bootstrap.ps1
./scripts/verify.ps1
```

Server 测试可单独运行：

```powershell
uv run --directory server ruff check .
uv run --directory server pytest
```

Desktop Reference Client 可单独初始化和验证：

```powershell
uv sync --directory clients/desktop_reference --locked --extra test
uv run --directory clients/desktop_reference ruff check src tests
uv run --directory clients/desktop_reference pytest -m "not e2e_host"
```

真实进程 E2E 会启动独立的 deterministic Director/Worker，不访问 provider；具体命令和环境变量见
[`clients/desktop_reference/README.md`](../../clients/desktop_reference/README.md)。

## 3. 启动 Server

```powershell
./scripts/run-local.ps1 -WorkerCount 2
```

检查 Director/Worker 的 `/health/live` 与 `/health/ready`。`live` 只证明进程存活；`ready` 还要检查配置、
coordination heartbeat 和启用的 provider/network policy。不得把 HTTP 200 单独当作语音闭环。

开发模式：

- `VOICE_RUNNER=deterministic`：验证协议、队列、lifecycle 和多 Worker，不访问 provider。
- `VOICE_RUNNER=livekit`：使用实际 FunASR/LLM/TTS；先单 Worker/WSS，再启用 UDP 或多实例。
- `VOICE_COORDINATION_BACKEND=memory`：单进程开发。
- `VOICE_COORDINATION_BACKEND=redis`：lease/fencing/grant 多实例验证。

## 4. Desktop 交互体验

桌面端与 ESP32 使用同一 `rva-control-v2` 和 media profiles。真实麦克风/扬声器必须显式安装 interactive extras，
并从 ignored `.env` 或进程环境提供 Director URL、bootstrap token 和唯一 device ID。CLI 不接受或输出 provider key；
provider 仍只由 Worker 配置。首轮建议强制 WSS，确认 host 音频闭环后再显式选择 UDP。

Desktop 的 headless/interactive 都不是独立业务 Agent，也不是 Server 替代品。headless host 通过只能证明当前 host
协议链路；interactive 通过不能证明 ESP32 的 AEC、VAD、唤醒词、UI、内存或弱网行为。

## 5. Native firmware

入口：`firmware/apps/voice_terminal`。激活锁定 ESP-IDF 后：

```powershell
Set-Location firmware/apps/voice_terminal
idf.py set-target esp32s3
idf.py build
idf.py size
```

Wi-Fi、Director URL 和 bootstrap token 使用 local Kconfig/build input，不进入 tracked defaults。配置改变后重新
configure/build，不能复用不匹配的 `sdkconfig`。烧录需要明确目标端口；记录 source identity、artifact digest、
board、IDF、命令和观察范围。

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
