# 兼容性基线

状态：superseded by ADR 0006
更新日期：2026-07-23

本基线保存 native endpoint 替代兼容实现时使用过的行为边界，不再是 current release gate。历史设备、
网络、端口、单次延迟和已被替代的固件制品不在 Product 文档中维护；当前证据见
[Release readiness](release-readiness.md)。

## 固定来源

- Upstream 与 dependency identity：`third_party/sources.lock.yaml`、`firmware/locks/`。
- 最小来源 provenance：`migration/baseline/source-manifest.yaml`。
- 回滚行为矩阵：`migration/baseline/behavior-matrix.yaml`。
- 当前 UDP byte authority 已迁移到 `protocol/udp_opus_gcm_v2/README.md` 与 canonical fixtures；迁移 manifest 中的
  旧路径只保留来源 provenance，不是 current consumer 入口。

## 必须保持的行为

| 边界 | Native parity 要求 |
| --- | --- |
| Board/UI | 屏幕、完整中文、触摸开始/停止、WSS/UDP mode |
| Config | Wi-Fi scan/save/fallback、endpoint/token origin、重启回读 |
| Audio | codec/TDM、ESP-SR `MR`、AEC reference、VAD、播放音量 |
| WSS | bootstrap/grant、Opus 上下行、字幕、TTS、cancel/reconnect |
| UDP | canonical VA header、AES-GCM、probe/source pin、replay、jitter/PLC |
| Lifecycle | single owner、generation fence、bounded queues/close、无旧输出复活 |

## 历史四象限

该阶段曾要求验证 legacy/new 四象限。ADR 0006 已接受 clean-slate v2，不再保留运行时 dual stack；当前门禁改为
v2 schema/fixtures 双端一致、旧 wire fail closed 和 fresh session upgrade。

## 历史退役门禁

Compatibility target/binding 已由 ADR 0006 决定退役。Git 历史和 migration provenance 承担追溯职责，不把旧实现
移动到 Product archive，也不恢复为 current 构建输入。
