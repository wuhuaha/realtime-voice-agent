# 决策 0002：Director 与 Worker 水平扩展

日期：2026-08-01
状态：accepted

## 决定

Product 使用 Session Director + stateful Realtime Worker 的部署边界。Director 负责 bootstrap、Worker registry、
capacity、route lease、fencing、connect grant 和 drain；Worker 独占一个 active session 的 WSS、UDP、AgentSession、
codec、playback generation 与 bounded teardown。Server 运行与生产编排只支持 Linux/container。

生产多实例使用 Redis-compatible shared store；memory backend 只允许测试和单进程开发。`VOICE_WORKER_MAX_SESSIONS=5`
是可配置的保守启动值，不是容量测量或 SLO。

## 约束

- 一个 active epoch 只有一个 Worker owner，WSS 与 UDP media 必须落在同一 Worker。
- Director 不读取、转发、缓存或重放媒体帧，不管理 Agent turn/provider stream；shared store 不进入 media hot path。
- Worker heartbeat 必须声明 public URL、active/max、profiles、healthy 和 draining；Director 只选择 heartbeat 有效、
  健康、non-draining、还有 slot 且 profile 相交的 Worker。
- Route lease 产生递增 fencing token；grant 绑定 Worker、device、epoch、fence、profiles、expiry 和 jti，并在共享
  coordination store 中单次消费。
- Worker 故障后设备 fresh bootstrap/session，不迁移 active turn；drain 是单向状态，恢复容量使用新的 Worker
  incarnation，不对旧进程 undrain。
- `/v2/voice` 是唯一设备语音入口；current wire 只有 `rva-control-v2`、`wss-opus-v3` 和 `udp-opus-gcm-v2`。

## 选择理由

媒体单跳且 ownership 清晰，Worker 可独立扩缩，Director 短时故障不会打断已建立媒体。普通无状态负载均衡、把媒体
放入 Redis/NATS/Kafka、或在 Worker 前增加媒体网关都会引入跨 owner、逐帧排队和额外生命周期故障域，暂不采用。

代价是每个 Worker 的 WSS/UDP endpoint 必须可达，证书、防火墙、service discovery、drain 和 Redis 运维需要明确配置。
真实容量、Redis HA、网络分区、故障演练和 provider/GPU 配额仍属于部署门禁，不由默认值推导。

## 复查条件

- 目标网络不能稳定暴露 Worker endpoint，需另行设计 Edge gateway。
- session 不再是合适的调度单位，需要 provider/GPU 独立配额。
- 需要 active turn 跨 Worker 无缝迁移，且业务价值足以承担分布式状态成本。

## 关联

- [Server 架构](../architecture/server.md)
- [部署指南](../operations/deployment.md)
- [安全模型](../security/security-model.md)
