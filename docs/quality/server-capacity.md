# Provider-free Server capacity

更新日期：2026-08-07

状态：`measured / bounded capacity baseline`

本文记录 RVA Server 协议与媒体层的 provider-free 容量基线。实验绕过 ASR、LLM、TTS 和 LiveKit Agent runtime，
使用 Python Desktop Reference Client 生成预编码 speech-like Opus 上行，并验证 deterministic 初始下行、playback
facts、route release 和资源回收。结论只用于 Server 接入层规划，不是完整语音 Agent 的生产容量 SLO。

## 当前结论

在本次 ol 共机环境与精确实验镜像上：

- WSS `wss-opus/1` 和 UDP `udp-opus-gcm/1` 均已验证 10、100 个活跃 session。
- 100 并发在单 Worker、`2 vCPU / 1 GiB` 下通过 5 分钟 steady 和 5 分钟 short-session churn；
  `2 vCPU / 2 GiB` 下的 130 并发 10 分钟余量验证也通过。
- 10 并发在 `1 vCPU / 512 MiB` 下通过 steady，但 5 分钟 churn 出现约 `40-53 MiB` Server cgroup swap，
  因而 512 MiB 不是统一最低配置。WSS/UDP 随后都在 `1 vCPU / 1 GiB` 通过 5 分钟 churn，并在
  `1 vCPU / 2 GiB` 通过 13 并发 10 分钟余量验证。
- 100 并发双 Worker 对照也通过，但 CPU 和内存占用均高于单 Worker；当前没有证据要求在 100 并发强制拆成
  两个 Worker。
- 有限容量阶梯已完成：WSS 单 Worker 为 200 通过、300 未通过；四 Worker 为 600 通过、800 未通过；
  UDP 单 Worker 为 150 通过、200 未通过；四 Worker 为 600 通过、800 未通过。该结果用于容量区间规划，
  不代表精确最大值，也不按 Worker 数线性外推。

## 10/100 配置基线

表中的“实验最低配置”只指 provider-free Server 协议与媒体层。接入真实 provider 后，必须额外计算 provider
连接、配额、GPU/模型和 Agent runtime 资源。

| 并发 | WSS / UDP | provider-free 实验最低配置 | 推荐配置 | Worker 数 | `max_sessions` | `nofile` | 带宽建议 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 10 | steady、churn 均 `measured` | `1 vCPU / 1 GiB` | `1 vCPU / 2 GiB`，13 并发 10 分钟已通过 | 1 | 最低 10；推荐 13 | PID 1 FD 合计峰值 104；保守最低 128，部署建议 4096 | 约 2 Mbps，按 steady Docker 观测峰值 2 倍取整 |
| 100 | steady、churn 均 `measured` | `2 vCPU / 1 GiB` | `2 vCPU / 2 GiB`，130 并发 10 分钟已通过 | 1 | 最低 100；推荐 130 | PID 1 FD 合计峰值 532；保守最低 1024，部署建议 4096 | 约 25 Mbps，按 steady Docker 观测峰值 2 倍取整 |

进程/线程数量是本实现的重要资源边界：10 并发 churn 已观测约 `3,473` PIDs/threads，100 并发 churn 峰值约
`6,086`。若部署编排器设置 `pids_limit`，10 并发实验最低建议不低于 `4,500`、标准档取 `8,192`；100 并发
最低建议不低于 `7,300`、推荐取 `16,384`。这些数值来自当前 PyAV/Agent-less test runtime，不应替代目标环境
复测。

## 实验身份与环境

| 项目 | 值 |
| --- | --- |
| Product base commit | `f53997b137c5fcbd97568c516e6c1a658679a78c` |
| 精确实验镜像 | `rva-capacity-exact:f53997b-b636abf` |
| 镜像 digest | `sha256:ca94ef44fd5c35c18fc9589e4f6fbcea0bd4e962dab629a254653ba06ee5f89d` |
| `server/uv.lock` SHA-256 | `6e14afd1f836f63f287cb2c8e77e980db97054c03d5ef6b59ff28d63728b6882` |
| 主机 | Tencent Cloud CVM，Ubuntu 24.04.4 LTS，Linux 6.8.0，AMD EPYC 9K65 |
| 主机资源 | 32 logical CPU，约 92 GiB RAM；模型与其他服务保持运行，代表共机场景下的保守结果 |
| 隔离 | Server 与 client generator 使用独立 Docker/cgroup v2 资源域；Server `nofile=65535` |
| 原始 artifact | `/srv/voice/benchmarks/rva-capacity/20260807T040819Z/`，不提交 Git |
| 归档包 | `artifacts-provider-free.tar.zst`，SHA-256 `c971e047cabc04dd57941491ecdb3c9ec7641987635b4a7fbd7063a04fb5543c` |

实验镜像包含当前容量工具的未提交实验快照，因此 base commit 不能单独恢复全部 harness；镜像 digest、lock digest
和 artifact 中的 source-file manifest 共同构成实验身份。该身份属于容量实验，不替代正式 release source/tag 身份。

## 已完成矩阵

### Steady

每个 session 按 60 ms cadence 持续发送预编码 Opus。成功要求 session 存活、媒体处理率至少 99.5%、初始下行与
playback facts 至少 99%、无系统性 stale/queue/media overload、最终 active session 为 0 且容器和端口回收。

| 场景 | 结果 | CPU p95 | 内存峰值 | FD / PIDs 峰值 | 连接 p95 |
| --- | --- | --- | --- | --- | --- |
| 10 WSS，1 Worker，`1 vCPU / 512 MiB` | `10/10`，媒体与初始下行 100% | 0.081 core | 204 MiB | 69 / 380 | 73 ms |
| 10 UDP，1 Worker，`1 vCPU / 512 MiB` | `10/10`，媒体与初始下行 100% | 0.066 core | 204 MiB | 70 / 380 | 75 ms |
| 100 WSS，1 Worker，`2 vCPU / 1 GiB` | `100/100`，媒体与初始下行 100% | 0.62 core | 376 MiB | 257 / 3,081 | 73 ms |
| 100 UDP，1 Worker，`2 vCPU / 1 GiB` | `100/100`，媒体与初始下行 100% | 0.51 core | 376 MiB | 257 / 3,081 | 93 ms |
| 130 WSS，1 Worker，`2 vCPU / 2 GiB`，10 分钟 | `130/130`，媒体与初始下行 100% | 0.71 core | 433 MiB | 320 / 3,982 | 152 ms |
| 130 UDP，1 Worker，`2 vCPU / 2 GiB`，10 分钟 | `130/130`，媒体处理 99.99%，初始下行 100% | 0.68 core | 434 MiB | 317 / 3,982 | 154 ms |

100 并发的 2 Worker 对照在同一 `2 vCPU / 1 GiB` 总配额下通过，但 WSS/UDP 内存峰值约 508 MiB，单 Worker
约 376 MiB；WSS CPU p95 约 0.73 core，单 Worker约 0.62 core。水平扩展能力需要由后续四 Worker阶梯验证，
不能只凭这一档宣称线性扩展。

### Short-session churn

Churn 重复建立和关闭短 session，用于覆盖 bootstrap、grant、route、媒体闭环和释放压力，不代表同一 session
连续存活。`stale_route_lease` 表示验证性 route reacquire 已推进 fencing 后，旧 session 被正确拒绝；它在 session
已完成 playback/release 的前提下是预期终态，不计为媒体或资源泄漏。

| 场景 | 结果 | CPU 配额 p95 | 最大单容器 cgroup peak / swap | PID 1 FD 合计 / Docker PIDs 峰值 |
| --- | --- | --- | --- | --- |
| 10 WSS，`1 vCPU / 512 MiB`，5 分钟 | 2,560/2,560 闭环，但因约 53 MiB swap 判为不通过 | 28.8% | 320 MiB / 53 MiB | 104 / 3,233 |
| 10 UDP，`1 vCPU / 512 MiB`，5 分钟 | 2,670/2,670 闭环，但因约 40 MiB swap 判为不通过 | 28.4% | 320 MiB / 40 MiB | 102 / 3,473 |
| 10 WSS，`1 vCPU / 1 GiB`，5 分钟 | 2,660/2,660 闭环，通过 | 27.3% | 365 MiB / 0 | 102 / 3,205 |
| 10 UDP，`1 vCPU / 1 GiB`，5 分钟 | 2,690/2,690 闭环，通过 | 28.2% | 349 MiB / 0 | 101 / 3,059 |
| 100 WSS，`2 vCPU / 1 GiB`，5 分钟 | 4,200/4,200 闭环，通过 | 55.7% | 661 MiB / 0 | 532 / 6,083 |
| 100 UDP，`2 vCPU / 1 GiB`，5 分钟 | 4,196/4,200 闭环，99.90%，通过 | 55.3% | 646 MiB / 0 | 502 / 6,086 |

UDP churn 的 4 次 client `TransportError` 未使成功率跌破 99% 门槛；Server 对 4,196 个成功 session 均形成关闭
事实，active session 最终归零，且没有 `media_overloaded`、queue full、OOM 或 Server cgroup swap。它是通过结果，
但不是零失败声明。

内存表中的 cgroup peak 是 Director、Redis、Worker 各容器峰值中的最大值，不是集群峰值之和；同时采样的
Docker current 合计在100并发churn约为720-730 MiB。FD数据是各容器PID 1的FD数量合计，不能解释为任一进程
实际同时打开的FD数；它用于保守推导`nofile`下限，正式部署仍建议使用4096或更高标准档。

## 网络口径

steady 的应用层 Opus 上行约为：10 并发 WSS `28 KB/s`、UDP `31 KB/s`；100 并发 WSS `284 KB/s`、UDP
`311 KB/s`。它不包含 HTTP/control、IP、TCP/UDP、TLS、代理与重传。

Docker 容器网络统计包含 Director/Redis 内部流量，并可能对 bridge 流量重复计数；观测到 10 并发单方向峰值约
`119 KB/s`、100 并发约 `1.27 MB/s`。配置表按其 2 倍向标准带宽档取整，故是保守规划值，不是公网链路精确预测。

## 有限容量阶梯

实验固定使用 16 vCPU / 16 GiB、30 秒预热、150 秒测量，provider 不访问。结果如下：

| Profile | Worker | 最高通过 | 首个未通过 | 主要信号 |
| --- | ---: | ---: | ---: | --- |
| WSS | 1 | 200 | 300 | 300 档出现 `opus_input_stale`/`media_overloaded` |
| WSS | 4 | 600 | 800 | 800 档四 Worker 均达到 200 session |
| UDP | 1 | 150 | 200 | 200 档媒体处理率 98.77%，低于 99.5% |
| UDP | 4 | 600 | 800 | 800 档媒体处理率 98.86%，低于 99.5% |

未继续执行 1600 及以上档位。更高容量、真实公网入口和 provider 负载仍需独立评估。

artifact SHA-256 manifest、压缩归档和环境清理属于实验交付收口项，原始数据保存在 ol benchmark 目录，不提交 Git。

## 适用边界

- WSS 走 ol loopback WebSocket，不包含公网 TLS、反向代理、WAF 或公网弱网成本。
- UDP 走同机 network namespace/bridge，不覆盖公网 NAT、防火墙和丢包抖动。
- 不包含 ASR、LLM、TTS、LiveKit Agent runtime、真实声卡、ESP32 或声学处理。
- steady 主要模拟设备持续聆听上行，只生成 deterministic 初始下行；不代表持续 TTS 下行带宽。
- 测试机器存在背景模型与服务负载，Server 已用 cgroup 限制，client generator 在独立资源域；系统背景噪声仍可能
  影响尾延迟。
- `VOICE_WORKER_MAX_SESSIONS=5` 仍是默认安全启动值。表中 10/100/130 是实验时显式配置，不应在未做目标环境
  验证时直接改为生产默认值。
