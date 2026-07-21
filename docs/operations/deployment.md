# 部署指南

状态：目标生产拓扑；执行前须满足 release gate
更新日期：2026-07-20

## 1. 推荐拓扑

```text
TLS/API gateway
  -> Session Director replicas
       -> Redis-compatible shared store
  -> Realtime Worker replicas
       -> fixed WSS endpoint per worker
       -> fixed UDP port per UDP-enabled worker
       -> ASR / LLM / TTS providers
```

Director 可多副本无会话媒体状态；Worker 是 stateful realtime unit。负载均衡不得把同一 WSS/UDP session 分到
不同 Worker。设备通过 Director bootstrap 获得 Worker 直达 endpoint。

## 2. 网络与端口

| 流向 | 端口/协议 | 要求 |
| --- | --- | --- |
| Device -> Director | HTTPS 443 | 稳定域名、TLS、bootstrap rate limit |
| Device -> Worker | WSS 443 或独立 TLS port | Worker-bound routing，禁止普通随机 LB |
| Device -> Worker | 固定 UDP port | 仅 UDP-enabled Worker；公网 advertise 与 NAT 映射一致 |
| Worker -> Director | HTTPS internal | heartbeat/drain，内部 token/mTLS policy |
| Director -> Redis | TCP/TLS internal | 私网、认证、备份/HA 按部署策略 |
| Worker -> Provider | HTTPS/WSS | egress allow-list、timeout、connection pool |

生产 UDP 不使用 bind/advertise port `0`。每个 Worker public WSS URL 和 UDP endpoint 必须从设备网络真实可达。

## 3. 配置

### Director

- `VOICE_COORDINATION_BACKEND=redis`，生产禁止 `memory`。
- `VOICE_REDIS_URL`、`VOICE_COORDINATION_PREFIX`。
- `VOICE_INTERNAL_TOKEN`、`VOICE_GRANT_SIGNING_KEY`、bootstrap credential。
- heartbeat/route lease TTL 按网络和 drain 策略配置；grant 不长于 route lease。

### Worker

- 唯一 `VOICE_WORKER_ID` 和可达 `VOICE_WORKER_PUBLIC_WS_URL`。
- 生产 `VOICE_WORKER_PUBLIC_WS_URL` 必须为 `wss://`；`ws://` 只允许受控开发环境。
- `VOICE_WORKER_MAX_SESSIONS` 默认 `5`，部署可覆盖；不是测量 SLO。
- `VOICE_DIRECTOR_URL`、heartbeat enabled/interval 和相同 signing/internal secret version。
- UDP bind/advertise host/port；未通过 UDP release gate 时禁用或强制 WSS。
- `VOICE_RUNNER=livekit` 与 provider endpoint/model/secret。

### Transport policy

初始生产策略保持 WSS baseline。即使 Worker 宣告 UDP capability，`auto/prefer_device` 也必须受 allow-list/灰度
控制；没有 HIL、弱网、安全和长稳证据前不得全量默认 UDP。

## 4. 启动顺序

1. Secret/Redis/TLS 可用。
2. 启动 Director，确认 `/health/live` 与 `/health/ready`。
3. 启动 Worker，确认 UDP socket（如启用）、local admission 和 heartbeat。
4. 从 Director internal workers API 确认 public URL、profiles、active/max、healthy、draining。
5. 使用 deterministic/reference client 做 bootstrap + WSS smoke。
6. 小流量 real-provider canary；再开放设备。

Readiness 当前不探测 provider network；部署系统必须单独观察 provider error/latency，不能仅靠 `/health/ready`。

## 5. Rolling update 与 drain

1. Director 将目标 Worker `draining=true`，确认不再分配新 session。
2. 等待 active sessions 自然归零，或达到配置的 drain deadline。
3. Deadline 到达后有界关闭：撤销 generation/UDP key、取消 Agent/provider task、关闭 transport。
4. 停止旧 Worker，部署新版本，检查 readiness/heartbeat。
5. Canary 通过后保留 replacement Worker；drain 是单向状态，不对旧进程执行 undrain。

Active turn 不迁移。设备在关闭后 fresh bootstrap，新 Worker 使用新 epoch/grant。

## 6. 扩缩容

- 扩容：启动新 Worker -> heartbeat ready -> Director 自动纳入候选。
- 缩容：先 drain，禁止直接 kill 仍持有 session 的 Worker。
- Director 选择按 `active/max` 和 stable tie-break，不代表实时 CPU safety；capacity 应结合 metrics/压测调整。
- Redis 故障时 Director fail closed，新 bootstrap 不成功；既有 Worker 媒体不访问 Redis。

## 7. 安全发布门禁

- 替换所有 placeholder，secret 来自 secret manager，不烘焙镜像。
- 生产禁用共享 lab token 或限定在隔离维护入口。
- Grant `jti` 由 Director 在 shared coordination store 原子消费；Redis-backed 重建与跨实例 replay 测试必须
  持续通过。该证据不替代 Redis HA、网络分区和生产故障演练。
- TLS、rate limit、UDP amplification/replay、日志脱敏和 retention 检查通过。
- 镜像/依赖 SBOM、license 和 vulnerability policy 完成。

## 8. 回滚

保留前一 Server image、protocol/profile allow-list、firmware artifact identity 和数据库/Redis key version。Server
回滚不得改变已发布 v1 wire；必要时先 drain 新 Worker，再恢复旧 Worker。Firmware 回滚必须验证 NVS 兼容，
不得擦除用户配置作为默认回滚步骤。

## 9. 当前发布状态

当前版本尚未通过正式 release gate。目标环境必须提供 HTTPS/WSS、受信域名/证书、secret manager、入口限流、
Redis durability/HA 策略和可观测性；临时主机地址、端口和本地 `.env` 不进入 Product 文档或 Git。精确门禁见
[Release readiness](../quality/release-readiness.md)。
