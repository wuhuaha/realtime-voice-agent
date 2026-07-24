# 部署指南

状态：目标生产拓扑；执行前须满足 release gate
更新日期：2026-07-24

## 0. Linux 单机交付基线

`deployment/single-node/` 提供一套版本受控的 Linux Docker Compose 资产，包含一个 Redis、一个 Session Director
和一个 Realtime Worker。它用于单机部署、集成验证和首版小容量交付，Worker capacity 默认 `5`，不是性能测量
结论。

仓库锁定 Python 依赖和基础镜像版本标签，但标签本身可变，因此默认配置不是 bit-for-bit reproducible。受控发布须把
`VOICE_SERVER_IMAGE` 与 `VOICE_REDIS_IMAGE` 都设置为 CI/发布流程验证并记录的 `image@sha256:<digest>`；不得在
没有实际 registry 证据时把示例 digest 当成发布身份。

该 Compose **不提供 TLS、证书管理、入口限流或 HA**。容器发布 Director HTTP `8080/tcp`、Worker
HTTP/WebSocket `8081/tcp` 和 Worker media `8092/udp`；公网设备使用的 `https://`/`wss://` 必须由仓库外的
受信 TLS gateway 终止并转发到这些 TCP 端口。UDP 端口不能由 HTTP gateway 代理，主机防火墙/NAT 必须把同一个
固定公网端口映射到 `8092/udp`。

### 0.1 主机前置条件

- Linux x86_64/arm64 主机、Docker Engine 和 Docker Compose `>=2.24.0`；目标架构必须存在依赖 wheel。
- 外部 TLS gateway 已配置域名与证书，支持 WebSocket upgrade；Compose 本身不证明这项能力。
- FunASR、LLM、TTS provider 可从 Worker 容器访问。
- `8080/tcp`、`8081/tcp` 的暴露范围按入口拓扑收敛。容器内部端口固定为 `8080/8081/8092`；
  `VOICE_UDP_PUBLIC_PORT` 同时控制主机 UDP publish port 和设备收到的 advertise port，NAT 外部端口也必须一致。

### 0.2 配置与启动

```bash
cd deployment/single-node
cp env.example .env
chmod 600 .env
# 编辑 .env：替换全部 replace-with-*、域名、device credentials 和 provider endpoint。
docker compose config --quiet
docker compose build --pull
docker compose up -d
docker compose ps
```

以上命令用于从当前 checkout 本地构建。使用已发布 digest 时，不重新构建：先设置两个 `image@sha256:<digest>`，再执行
`docker compose pull` 和 `docker compose up -d --no-build`，并把实际 digest 与 Product commit 一起记录。

`.env` 被 Git 忽略，镜像构建也不会读取它。Compose 对 Director 与 Worker 使用不同的显式环境白名单：Director
获得 device credentials/Redis 配置但不获得 provider key，Worker 获得 provider 和内部授权配置但不获得 device
credentials/Redis URL。启动前置检查和应用 runtime validation 都会拒绝 `replace-with-*` 占位值。不得把
`docker compose config` 的完整展开输出写入日志或工单，因为 Compose 会展开环境变量。更新 secret 时修改主机
`.env` 后重建受影响容器；不要把 secret 作为 Dockerfile `ARG`、镜像 `ENV` 或命令行参数。

Redis 以镜像内置的 `redis` 用户运行，避免 `cap_drop: ALL` 时 root entrypoint 尝试 chown/gosu。官方镜像的 `/data`
目录为该用户准备，新的 named volume 会从镜像挂载点初始化 ownership；从旧部署复用外部 volume 时，须先离线确认
其 UID/GID 可写。Worker 不加入 Redis 所在的 `coordination` 网络，只能经 `worker-control` 访问 Director。

### 0.3 健康与烟测

```bash
curl --fail http://127.0.0.1:8080/health/ready
curl --fail http://127.0.0.1:8081/health/ready
docker compose ps
docker compose logs --tail=100 director worker
```

Worker readiness 包含 provider endpoint 的有界 DNS/TCP/TLS 网络连通探测、Director heartbeat 和 UDP socket
（启用时）。网络探测不会发送合法 provider 请求，也不验证鉴权、模型存在性、配额、请求/响应 schema 或语义质量；
`healthy` 只说明这些有限门禁通过，不代表 provider 可完成推理，也不代表 TLS 公网入口、真机语音、声学、弱网或
容量已验证。日志不得粘贴到公开渠道；应用设计上不输出 secret，但运行环境和第三方错误仍按敏感运维数据处理。

### 0.4 数据、停止与升级

Redis AOF 位于 Compose named volume `redis-data`。普通停止使用 `docker compose down`，不会删除该 volume；
`docker compose down -v` 会删除 coordination 数据，只能在明确接受会话 lease/grant 状态丢失时使用。

单 Worker 升级无法做到媒体会话无中断。先调用 drain、等待 active session 归零，再按第 5 节为 replacement 分配
新的 `worker_id` 并部署；不得用相同 incarnation 直接 `docker compose up -d` 或 `systemctl restart`。该基线的
`restart: unless-stopped` 是同一进程实例的恢复策略，不是 HA；主机、磁盘、Redis 或唯一 Worker 故障都会造成服务
降级或中断。

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
- `VOICE_REDIS_URL`、`VOICE_COORDINATION_PREFIX`，以及 Redis connect/command deadline；默认
  `VOICE_REDIS_CONNECT_TIMEOUT_SECONDS=1`、`VOICE_REDIS_COMMAND_TIMEOUT_SECONDS=1`。单机 Compose 内部固定由
  Director 使用，不注入 Worker。
- `VOICE_INTERNAL_TOKEN`、`VOICE_GRANT_SIGNING_KEY`、bootstrap credential。
- heartbeat/route lease TTL 按网络和 drain 策略配置；grant 不长于 route lease。

### Worker

- 唯一 `VOICE_WORKER_ID` 和可达 `VOICE_RVA_PUBLIC_WS_URL`。
- 生产 `VOICE_RVA_PUBLIC_WS_URL` 必须为 `wss://` 且 path 为 `/v2/voice`；`ws://` 只允许受控开发环境。
- `VOICE_WORKER_MAX_SESSIONS` 默认 `5`，部署可覆盖；不是测量 SLO。
- `VOICE_DIRECTOR_URL`、heartbeat enabled/interval 和相同 signing/internal secret version。
- `VOICE_SHUTDOWN_DRAIN_TIMEOUT_SECONDS` 控制关停总预算，默认 10 秒；部署 orchestrator 的 termination grace period
  必须大于该预算并留出进程退出余量。
- `VOICE_AGENT_CLOSE_STAGE_TIMEOUT_SECONDS` 限制 session、output 和 TTS 等单个 cleanup 阶段，默认 2 秒；部署时应
  明显小于关停总预算，用于隔离不响应取消的 provider/runner，不能视为正常请求 deadline。
- UDP bind/advertise host/port；单机 Compose 内部 bind 固定 `8092`，公网 publish 与 advertise 共用
  `VOICE_UDP_PUBLIC_PORT`；未通过 UDP release gate 时禁用或强制 WSS。
- `VOICE_RUNNER=livekit` 与 provider endpoint/model/secret。
- `VOICE_TTS_QUEUE_TIMEOUT_SECONDS` 控制 TTS bulkhead 排队时间，默认 0.25 秒；增大它会把过载转为对话延迟，不能
  作为扩容替代品。

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

Readiness 只探测 provider endpoint 的网络连通性，不证明 provider API 语义。部署系统必须另外执行带真实凭据的
小流量 canary，并观察 provider error/latency；不能仅靠 `/health/ready` 放量。

## 5. Rolling update 与 drain

1. Director 将目标 Worker `draining=true`，确认不再分配新 session。
2. 等待 active sessions 自然归零，或达到配置的 drain deadline。
3. Deadline 到达后有界关闭：撤销 generation/UDP key、取消 Agent/provider task、关闭 transport；registry 关闭后
   在总预算内通过最多 32 次 draining heartbeat 分批确认 exact lease release。
4. 为 replacement 生成唯一 `worker_id`，部署新版本，检查实际 `EnvironmentFile`、readiness 和 heartbeat 均指向新
   incarnation；不要只依据 systemd drop-in 中可能被覆盖的 `Environment=` 值。
5. Canary 通过后保留 replacement Worker；drain 是单向状态，不对旧进程执行 undrain。

Active turn 不迁移。设备在关闭后 fresh bootstrap，新 Worker 使用新 epoch/grant。

同一个 `worker_id` 的 drain 标记会在 Redis 中保持到 heartbeat TTL 到期。单机使用相同 `worker_id` 原地替换时，
不得直接 `systemctl restart`：旧进程关闭前会发布 `draining=true`，新进程随后会继承该状态并持续返回 readiness
503。应为每个 replacement 生成唯一 incarnation ID，待其 ready 后再回收旧实例。仅在无法更换 ID 的恢复场景，
才停止旧 Worker、等待超过 `worker_heartbeat_ttl_seconds` 并留出调度余量，再启动 replacement；启动后还必须从
registry/readiness 确认 sticky drain 已消失。v1 drain API 只允许进入 drain，不提供 undrain。

UDP grant 同时携带绝对安全边界 `expires_at_ms` 和相对调度值 `refresh_after_ms`。Endpoint 必须用 monotonic clock 在
`refresh_after_ms` 到达时 normal close 并 fresh bootstrap；该轮换不依赖 SNTP，且不得复用旧 epoch、key、generation
或 sequence。Server 仍以 `expires_at_ms` 拒绝过期 datagram。到期轮换属于预期生命周期，不得上报为
`protocol_error`；部署验收必须跨越至少一次 refresh 边界并观察新 session 正常收发。

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

保留前一 Server image、protocol/profile allow-list、firmware artifact identity 和数据库/Redis key version。回滚只能
恢复彼此匹配并通过门禁的 v2 Server/Firmware artifact；不得用 v1 wire 或仅单边回滚制造不兼容组合。必要时先 drain
新 Worker，再恢复旧 Worker。Firmware 回滚必须验证 NVS 兼容，不得擦除用户配置作为默认回滚步骤。

## 9. 当前发布状态

当前版本尚未通过正式 release gate。目标环境必须提供 HTTPS/WSS、受信域名/证书、secret manager、入口限流、
Redis durability/HA 策略和可观测性；临时主机地址、端口和本地 `.env` 不进入 Product 文档或 Git。精确门禁见
[Release readiness](../quality/release-readiness.md)。

Runbook 不保存某台主机的当前 release 快照。每次部署必须形成一条 release record，并在
[Release readiness](../quality/release-readiness.md) 或受控发布系统中记录以下不可变身份：

| 字段 | 要求 |
| --- | --- |
| `product_commit` | 完整 40 位 Product Git commit |
| `archive` / `archive_sha256` | archive 名称及发布前计算的 SHA256；上传、下载和解包前后不得改变 |
| `release_dir` | 包含 commit identity 的不可变绝对目录；不得原地覆盖已有 release |
| `worker_id` | 每次 replacement 唯一的 incarnation，不复用旧 Worker ID |
| `config_identity` | 实际生效 `EnvironmentFile`/secret version 的受控引用，不记录 secret 值 |
| `server_image_digest` | 容器部署时使用实际 `image@sha256:<digest>`；源码部署记为不适用 |

激活前先按发布记录校验 archive：

```bash
printf '%s  %s\n' "$RELEASE_ARCHIVE_SHA256" "$RELEASE_ARCHIVE" | sha256sum --check --strict -
test ! -e "$RELEASE_DIR" || { echo "release directory already exists" >&2; exit 1; }
```

解包和依赖恢复完成后，以只读方式创建 release 目录；再核对 service manager 实际加载的 `EnvironmentFile`、
`VOICE_WORKER_ID` 和 source/image identity。激活后必须同时检查 Director/Worker readiness、heartbeat 中的 Worker
incarnation、UDP ready（启用时）和 real-provider canary。readiness 只证明基础服务状态，ESP32 媒体 HIL、长稳和
声学门禁仍须独立执行并写入 release record；未完成时只能标记为验证环境。

设备 HIL 与持续联调应始终使用稳定的公网 bootstrap origin，并通过 provisioning/受控配置绑定 credential；不得随
开发者本机网络变化反复重编固件。真实域名、主机 inventory、网络凭据和 token 只保存在受控部署资产中。
