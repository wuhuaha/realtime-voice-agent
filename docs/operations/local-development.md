# 本地开发

更新日期：2026-07-21

## 1. 前置条件

- Windows PowerShell 7、Git、Python 3.12、`uv`。
- Firmware 使用锁定 ESP-IDF 5.5.2。
- 多实例 coordination 使用 Redis-compatible service；memory backend 只用于单进程开发。
- 真实 provider 与设备凭据只写 ignored `.env` / local build input。

## 2. 初始化与验证

```powershell
Copy-Item .env.example .env
./scripts/bootstrap.ps1 -SkipFirmware
./scripts/verify.ps1
```

Server 测试可单独运行：

```powershell
uv run --directory server ruff check .
uv run --directory server pytest
```

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

## 4. Native firmware

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

`firmware/targets/lichuang-dev` 只用于显式 compatibility/rollback，不接受 native 新功能，也不参与默认验证或发布。

## 5. 最小开发顺序

1. 根 repository/protocol gate。
2. Server deterministic unit/contract。
3. Director + Worker WSS host E2E。
4. 单 Worker真实 provider smoke。
5. Native clean build/size。
6. 授权后 WSS HIL。
7. UDP tamper/replay/weak-network 与 HIL。
8. Redis multi-worker、声学和长稳。

实际状态见 [Release readiness](../quality/release-readiness.md)。
