# realtime-voice-agent

面向低资源嵌入式设备、浏览器和移动端的实时语音 Agent 端云工程。首个交付 endpoint 是立创实战派
ESP32-S3：通过 Xiaozhi WSS control 与 `wss-opus-v1` / `udp-opus-gcm-v1` 媒体 profile 接入 roomless
LiveKit `AgentSession`；服务端由 Session Director 与可水平扩展的 Realtime Worker 组成。

## 当前迁移状态

- 来源基线固定在 `voice-agent-research` 的已审计提交，详见 `migration/baseline/source-manifest.yaml`。
- Direct WebRTC、AIMP、PCM DataChannel 和研究归档代码不进入本仓。
- `VOICE_WORKER_MAX_SESSIONS` 默认 `5`，只是可覆盖的保守启动值，不是容量测量或 SLO。
- 自动化、真机、声学和公网结果分别记录；未运行项不会标记为通过。

## 目录

```text
protocol/       canonical control/media contracts and fixtures
server/         session_director, realtime_worker, shared contracts and tests
firmware/       reproducible ESP32 source materialization and target components
docs/           product, architecture, protocol, security, quality and operations
tests/          cross-endpoint compatibility, e2e and HIL entry points
migration/      immutable source provenance and behavior baseline
third_party/    pinned upstream sources and license inventory
```

## 本地入口

```powershell
./scripts/bootstrap.ps1
./scripts/verify.ps1
./scripts/run-local.ps1
```

真实 provider 和设备配置写入 ignored `.env` / `.env.local`。模板只保存字段和安全占位符。
