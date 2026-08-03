# v0.1.0-alpha Release notes

状态：release candidate / not release-ready

`v0.1.0-alpha` 是 realtime-voice-agent 的首个开源技术预览。它聚焦低资源 endpoint 与 LiveKit Agents 之间的
roomless 实时语音接入，不把具体闲聊业务、模型效果或完整 RTC 能力作为产品核心。

## 主要内容

- 定义单一 current wire：`rva/1`、`/rva/v1/voice`、`wss-opus/1` 和 `udp-opus-gcm/1`。
- 提供 native ESP-IDF ESP32-S3 reference endpoint，分离 board、audio/AFE、transport、configuration 和可选 LVGL UI。
- 提供 Python Desktop Reference Client，用于确定性 WSS/UDP host E2E 和显式启用的本机音频体验。
- 提供 Session Director、可横向扩展的 Realtime Worker、Redis coordination 和 roomless LiveKit `AgentSession` binding。
- 实现 worker-bound grant、single-use admission、lease/fencing、capacity、drain、playback generation 与有界 teardown。
- WSS 作为 baseline；UDP profile 使用每 session 双向 AES-GCM key/salt、authenticated probe、replay window、
  小型 jitter/playout queue、freshness 和 generation fence。
- 提供 FunASR、DeepSeek-compatible LLM、CosyVoice 和 MiMo TTS reference adapters。
- 提供协议 fixtures、repository contracts、Server/Reference Client tests、Redis process E2E、native host contracts 和
  固定 ESP-IDF build/size gate。

## 当前验证摘要

- 历史候选 GitHub Actions 的 repository、server、desktop reference、host E2E、Redis integration、native host contracts
  和 ESP-IDF build/size jobs 已通过；当前 freshness 修复的本地自动门禁已通过，commit-addressable CI 尚未运行。
- 公网 Linux Director/Worker/Redis readiness、real-provider desktop canary和 ESP32-S3 UDP 双向 Opus 真机闭环已有
  commit-addressable 证据。历史 WSS artifact虽完成媒体播放，但随后触发 freshness regression；修复源码已通过 host
  回归，最终 artifact WSS HIL 仍为 `not_run`。
- 真机验证覆盖 bootstrap、UDP authenticated probe/source pinning、完整 playback fact、normal close 和 exact route
  release；未把 host build 或旧日志当作当前 HIL。

精确 commit、artifact digest、测试数量和仍未执行的门禁只在
[Release readiness](release-readiness.md) 维护，避免 release notes 复制易过期的瞬时数据。

## 发布边界

本版本不是 production-ready 声明。Linux/TLS/HA、UDP opt-in、弱网、长稳、延迟、声学和 provider 责任边界见
[Known limitations](known-limitations.md)。正式 tag 前仍需从最终 Product commit fresh 构建公共无凭据 firmware、
Server artifact 和 release SBOM，并在最终 artifact 上完成最小 WSS smoke；UDP 当前证据仅对历史已测 artifact 有效，
若最终改动影响其路径则同时复验 UDP。

## SBOM

发布候选从锁文件确定性生成 CycloneDX 1.5 SBOM：

```bash
uv run python scripts/generate_release_sbom.py --output artifacts/release-sbom.cdx.json
uv run python scripts/generate_release_sbom.py --output artifacts/release-sbom.cdx.json --check
```

SBOM 省略运行时间戳和随机 serial，并记录每个输入 lock 的 SHA-256。它不替代目标二进制的漏洞扫描或许可证复核。
