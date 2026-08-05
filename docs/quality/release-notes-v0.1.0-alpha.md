# v0.1.0-alpha Release notes

状态：release candidate / not release-ready

> 本文的设备与artifact摘要描述上一可恢复alpha baseline，不代表当前未提交工作快照已经release-ready。
> 当前身份、门禁和未运行项以 [Release readiness](release-readiness.md) 为准。

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

- GitHub Actions 的 repository、server、desktop reference、host E2E、Redis integration、native host contracts和
  ESP-IDF build/size jobs已通过；当前 Server source `3f207a5` 的本地完整门禁与 commit-addressable CI 7/7已通过。
- 公网 Linux Director/Worker/Redis readiness、real-provider desktop canary和 ESP32-S3 UDP 双向 Opus 真机闭环已有
  commit-addressable 证据。历史 WSS artifact触发的 freshness regression已修复；当前 Server/Firmware artifact完成
  WSS 真机完整播放、normal close、exact release和零 overload门禁。
- 当前 WSS/UDP 真机验证均绑定同一 Server/Firmware 组合，覆盖 bootstrap、双向 Opus、完整 playback fact、normal close
  与 exact route release；UDP还覆盖 authenticated probe和source pinning，当前两条链路均为 `device_verified`。
- 当前 clean Product source已完成公共空凭据 ESP32-S3 bundle的可复现构建、size、五分区打包、provenance、许可证声明
  和 SHA-256校验；CycloneDX 1.5 release SBOM已确定性生成并通过 `--check`。公共镜像通过临时本地NVS provisioning
  完成启动、触屏/语音唤醒、WSS/UDP双协议问答、normal close和exact release真机门禁，测试凭据未进入bundle或Git。

精确 commit、artifact digest、测试数量和仍未执行的门禁只在
[Release readiness](release-readiness.md) 维护，避免 release notes 复制易过期的瞬时数据。

## 发布边界

本版本不是 production-ready 声明。Linux/TLS/HA、UDP opt-in、弱网、长稳、延迟、声学和 provider 责任边界见
[Known limitations](known-limitations.md)。公共 firmware bundle和 release SBOM候选已生成，但尚未创建正式 tag或
GitHub Release；若 tag source不同于当前 bundle source，必须从 tag source重新构建。当前 private deployment image的
真机证据不替代公共 bundle验证；最终release artifact仍以其内置manifest、provenance和GitHub Release digest为准。

## SBOM

发布候选从锁文件确定性生成 CycloneDX 1.5 SBOM：

```bash
uv run python scripts/generate_release_sbom.py --output artifacts/release-sbom.cdx.json
uv run python scripts/generate_release_sbom.py --output artifacts/release-sbom.cdx.json --check
```

SBOM 省略运行时间戳和随机 serial，并记录每个输入 lock 的 SHA-256。它不替代目标二进制的漏洞扫描或许可证复核。
