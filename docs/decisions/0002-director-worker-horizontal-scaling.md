# 决策 0002：Director 与 Worker 水平扩展

日期：2026-07-20
状态：accepted
决定：首版交付 Session Director + stateful Realtime Worker；Director 不接触媒体，生产使用 Redis-compatible
shared store，Worker `max_sessions` 默认 `5` 且可配置。

## 背景

WSS control 与 UDP media 必须落在同一个 active session owner。普通无状态多副本或仅按 WSS sticky 不能可靠
处理 UDP `media_id`、duplicate device、drain、capacity 和故障 fencing。用户明确要求首版包含水平扩展入口。

## 已考虑选项

### 单 Runtime 多副本 + 普通负载均衡

不选择。WSS/UDP affinity、双 owner、全局 admission 和 drain 语义不闭合。

### Director + stateful Worker

选择。设备 bootstrap 后直连一个 Worker，媒体单跳；Director只负责 registry、capacity、lease/fencing、grant
和 drain。

### Edge Media Gateway + Worker

暂不选择。它统一公网入口但增加媒体一跳、IPC、双层 lifecycle和新故障域；仅当 Worker endpoint无法可靠
暴露时复查。

### 音频经 Redis/NATS/Kafka

排除。可靠消息系统不适合逐帧实时音频，会引入复制、排队和 stale media。

## 证据

- [Server 架构](../architecture/server.md)。
- `voice_contracts` 中 Worker heartbeat、route lease、grant claims。
- `session_director` 的 memory/Redis coordination adapters。
- [协议总览](../protocol/overview.md) 的同 Worker control/media ownership。
- 当前 Server `d2fa0ca` 的 Redis-enabled pytest `179 passed`，覆盖 atomic lease/fencing、heartbeat/drain 并发、
  shared `jti` 单次消费、Redis-backed service/store 重建后 replay 拒绝和跨实例行为。

## 决定与范围

- 一个 active epoch 只有一个 Worker owner，完整持有 WSS、UDP、AgentSession、codec、generation 和 cleanup。
- Director 不读取、转发、缓存或重放媒体帧，不管理 Agent turn/provider stream。
- Worker heartbeat报告 public URL、active/max、profiles、healthy和 draining。
- Director 只选择 heartbeat有效、健康、non-draining、有 slot且 profile相交的 Worker。
- Route lease产生递增 fencing token；grant绑定 Worker/device/epoch/fence/profiles/expiry/jti。
- 生产多实例使用 Redis-compatible shared store；memory只允许测试和单进程开发。
- Shared store 不进入 media hot path。
- `VOICE_WORKER_MAX_SESSIONS=5` 是保守可配置默认值，不是容量测量或 SLO。
- Worker失败后 fresh bootstrap/session，不迁移 active turn。
- Drain 为当前 Worker 进程的单向状态；需要恢复容量时启动 replacement Worker，不对原进程 undrain。

## 后果与风险

正面：媒体单跳、ownership 清晰、Worker可独立扩缩、Director短时故障不打断既有媒体。代价：每个 Worker
WSS/UDP endpoint必须设备可达，证书、防火墙、service discovery和 drain运维更复杂。

风险：Redis lease、grant 单次消费和 Worker active count 的实现缺陷仍可能产生错误 admission。当前实现已由
Director 在 shared coordination store 原子消费 `jti`，并已有 Redis 重启/跨实例测试，不再是 process-local
replay guard；但 Redis HA、网络分区、真实多进程压力与生产故障演练尚未完成。默认容量被误解仍会造成过载，
必须用目标环境压测调整。

Repair commit `259aeee` 已使 Worker readiness 依赖 coordination heartbeat，并让 drain request 仅允许 `true`；
`d2fa0ca` 强制 public endpoint 为绝对 canonical URL、production 使用 `wss://`，并修复 launcher process
identity 与停止失败保留语义；精确 tick 路径的 local stop/start 已实测通过。

## 兼容和迁移

开发可保留 direct Worker + lab token 路径用于 reference baseline，但不能作为生产水平扩展证明。设备完成
bootstrap能力迁移后，应优先使用短期 worker-bound grant。WSS/UDP wire保持 v1，不因部署拓扑改变。

## 复查触发条件

- 目标网络不能稳定暴露 Worker WSS/UDP endpoint。
- Redis-compatible store不能满足 atomic lease/fencing或成为媒体同步依赖。
- 新增跨区域、边缘入口或标准浏览器媒体需要统一 Edge。
- 容量测量表明 session不是合适调度单位，需引入 provider/GPU独立配额。
- 需要 active turn无缝迁移，且业务价值足以承担分布式状态成本。

## 关联链接

- [系统架构](../architecture/system.md)
- [部署指南](../operations/deployment.md)
- [安全模型](../security/security-model.md)
