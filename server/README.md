# Realtime Voice Agent Server

本目录是独立的 Python 3.12 `uv` workspace，包含两个部署单元和两个共享包：

```text
apps/session_director   稳定 bootstrap、worker registry、capacity、lease/fencing、grant、drain
apps/realtime_worker   RVA WSS/UDP、roomless LiveKit Agent、FunASR/DeepSeek/TTS
packages/voice_contracts  Director/Worker 边界模型与 HMAC grant
packages/voice_testkit    确定性测试时钟和后续 host fake
```

ESP32 路线不需要 LiveKit Server 或 Room。`livekit-agents` 只在 worker 内作为 roomless voice runtime；
Director 不导入 LiveKit、AV、Opus或 provider，媒体建立后不访问 coordination store。

## 安装和验证

```powershell
cd server
uv lock --check
uv sync --locked --all-packages --dev
uv run ruff check .
uv run pytest --timeout=20 --timeout-method=thread
```

Redis integration 默认不访问本机服务。对专用测试实例显式运行：

```powershell
$env:VOICE_TEST_REDIS_URL = "redis://127.0.0.1:6399/15"
uv run pytest apps/session_director/tests/test_redis_store.py
```

## 运行入口

```powershell
uv run session-director
uv run realtime-worker
```

根 `scripts/run-local.ps1` 会调用本目录 launcher。它为每个 Worker 分配独立 HTTP/UDP端口，以隐藏进程
启动并把 PID、端口和日志路径写入 `.runtime/local/server-processes.json`：

```powershell
./scripts/run-local.ps1 -WorkerCount 2
./server/scripts/run-local.ps1 -Stop -RuntimeDirectory ./.runtime/local
```

Director API：

```text
POST /v1/session/bootstrap
POST /v1/session/release
POST /internal/v1/workers/heartbeat
POST /internal/v1/workers/{worker_id}/drain
GET  /internal/v1/workers
GET  /health/live
GET  /health/ready
```

Worker API：

```text
WS   /v2/voice
POST /internal/v1/drain
GET  /health/live
GET  /health/ready
```

`/v2/voice` 是唯一设备语音入口。当前 runtime 不注册 `/v1/voice` 或 `/v1/xiaozhi`，协议升级通过 fresh session
完成，不在同一进程保留 dual stack。

Worker `max_sessions` 默认 `5`，可用 `VOICE_WORKER_MAX_SESSIONS` 覆盖。生产多 Director/多 Worker 必须使用
`VOICE_COORDINATION_BACKEND=redis`；memory backend只允许测试和单进程开发。

Worker 在开发模式可接受实验室共享 token，也接受 Director 签发的 worker/device/session epoch/fencing/profile/jti/expiry
绑定 grant。Worker 连接前通过 Director 在 shared coordination store 原子消费 grant；WSS和所选 UDP media
始终由同一 worker持有。

Director 对 Redis 连接和单条命令分别应用 `VOICE_REDIS_CONNECT_TIMEOUT_SECONDS` 与
`VOICE_REDIS_COMMAND_TIMEOUT_SECONDS`（默认均为 1 秒）；超时对外统一为脱敏的
`503 coordination_unavailable`。设备本地建链失败或完整停止后调用认证的 `/v1/session/release`，Director 仅释放
完全匹配 worker/epoch/fencing 的 route，重复或 stale release 仍返回幂等成功。

Worker 关停总预算由 `VOICE_SHUTDOWN_DRAIN_TIMEOUT_SECONDS` 控制（默认 10 秒）。它先发布 sticky drain，再关闭
session registry，并在预算内用有界 heartbeat 批次上报 exact lease release；耗尽预算或最大尝试次数时记录 pending
数量后退出，不无界等待 Director。单个 Agent cleanup 阶段另受
`VOICE_AGENT_CLOSE_STAGE_TIMEOUT_SECONDS`（默认 2 秒）约束，防止不响应取消的 provider 或 runner 穿透总预算。
三个 TTS adapter 共用 `VOICE_TTS_QUEUE_TIMEOUT_SECONDS`（默认 0.25 秒）的
并发槽排队上限；超时按可重试背压处理。MiMo SSE parser 同时限制单行/单事件 1 MiB、每事件 256 条 data line、
单个解码音频 chunk 512 KiB 和整次响应 8 MiB。

真实 provider、真机、声学、弱网、公网和长稳不属于默认 pytest，未显式运行时均为 `not_run`。当前 Server
完整 suite 与集成状态见 [Release readiness](../docs/quality/release-readiness.md)。
