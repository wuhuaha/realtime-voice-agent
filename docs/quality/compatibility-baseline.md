# 兼容性基线

状态：rollback oracle
更新日期：2026-07-21

本基线只定义 native endpoint 替代兼容实现时必须保持的行为边界，不记录实施阶段或运行编年。历史设备、
网络、端口、单次延迟和已被替代的固件制品不在 Product 文档中维护；当前证据见
[Release readiness](release-readiness.md)。

## 固定来源

- Upstream 与 dependency identity：`third_party/sources.lock.yaml`、`firmware/locks/`。
- 最小来源 provenance：`migration/baseline/source-manifest.yaml`。
- 回滚行为矩阵：`migration/baseline/behavior-matrix.yaml`。
- 当前 UDP byte authority：`protocol/udp_opus_gcm_v1/README.md` 与 canonical fixtures；迁移 manifest 中的旧路径只
  保留来源 provenance，不是当前 consumer 入口。

## 必须保持的行为

| 边界 | Native parity 要求 |
| --- | --- |
| Board/UI | 屏幕、完整中文、触摸开始/停止、WSS/UDP mode |
| Config | Wi-Fi scan/save/fallback、endpoint/token origin、重启回读 |
| Audio | codec/TDM、ESP-SR `MR`、AEC reference、VAD、播放音量 |
| WSS | bootstrap/grant、Opus 上下行、字幕、TTS、cancel/reconnect |
| UDP | canonical VA header、AES-GCM、probe/source pin、replay、jitter/PLC |
| Lifecycle | single owner、generation fence、bounded queues/close、无旧输出复活 |

## 四象限

协议或 transport 发生兼容变化时分别验证 legacy client/legacy server、legacy client/new server、native client/
legacy-compatible server、native client/new server。`rva-control-v1` 是新控制协议，不要求 wire 兼容 Xiaozhi；
共享的 `udp-opus-gcm-v1` 必须保持逐字节兼容。

## 退役门禁

Native clean build、WSS/UDP 真机闭环、UI/config、AEC/打断、20 轮、30 分钟和最终 repository gate 全部通过后，
才可按支持策略删除 compatibility target/binding。Git 历史承担归档职责，不把旧实现移动到 Product archive。
