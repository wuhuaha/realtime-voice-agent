# realtime-voice-agent

面向低资源嵌入式设备、浏览器和移动端的实时语音 Agent 端云工程。首个交付 endpoint 是立创实战派
ESP32-S3：通过 Xiaozhi WSS control 与 `wss-opus-v1` / `udp-opus-gcm-v1` 媒体 profile 接入 roomless
LiveKit `AgentSession`；服务端由 Session Director 与可水平扩展的 Realtime Worker 组成。

## 当前迁移状态

- 来源基线固定在 `voice-agent-research` 的已审计提交，详见 `migration/baseline/source-manifest.yaml`。
- `firmware/targets/lichuang-dev` 是唯一 production firmware composition；源码已收口不代表发布就绪。
- `firmware/device` 仅用于 component extraction prototype，不生成 release firmware。
- Direct WebRTC、AIMP、PCM DataChannel 和研究归档代码不进入本仓。
- `VOICE_WORKER_MAX_SESSIONS` 默认 `5`，只是可覆盖的保守启动值，不是容量测量或 SLO。
- 自动化、真机、声学和公网结果分别记录；未运行项不会标记为通过。
- 公网评测部署和历史 `cb544...` ESP32 artifact 已完成 WSS real-provider 闭环；superseded `9026...` 在
  `0014` 中间态复验到 WSS/AFE AEC/ASR/流式字幕。当前 `61542...` 已完成独立 public WSS/ASR/TTS/playout
  smoke。该环境没有受信 TLS；当前 artifact UDP HIL、UI/触摸、
  正式声学、弱网与长稳仍未完成，不能标记 production ready。

## 目录

```text
protocol/       canonical control/media contracts and fixtures
server/         session_director, realtime_worker, shared contracts and tests
firmware/       production target composition and non-release component prototype
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
