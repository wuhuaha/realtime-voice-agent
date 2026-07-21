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
POST /internal/v1/workers/heartbeat
POST /internal/v1/workers/{worker_id}/drain
GET  /internal/v1/workers
GET  /health/live
GET  /health/ready
```

Worker API：

```text
WS   /v1/voice
WS   /v1/xiaozhi  (legacy compatibility only)
POST /internal/v1/drain
GET  /health/live
GET  /health/ready
```

Worker `max_sessions` 默认 `5`，可用 `VOICE_WORKER_MAX_SESSIONS` 覆盖。生产多 Director/多 Worker 必须使用
`VOICE_COORDINATION_BACKEND=redis`；memory backend只允许测试和单进程开发。

Worker兼容实验室共享 token，也接受 Director 签发的 worker/device/session epoch/fencing/profile/jti/expiry
绑定 grant。Worker 连接前通过 Director 在 shared coordination store 原子消费 grant；WSS和所选 UDP media
始终由同一 worker持有。

真实 provider、真机、声学、弱网、公网和长稳不属于默认 pytest，未显式运行时均为 `not_run`。当前 Server
完整 suite 与集成状态见 [Release readiness](../docs/quality/release-readiness.md)。
