# 测试策略

状态：accepted
更新日期：2026-07-20

## 1. 证据词汇

| 状态 | 可声称 | 不可声称 |
| --- | --- | --- |
| `unit_verified` | 隔离逻辑在给定输入下通过 | 组件集成或真实 I/O 可用 |
| `contract_verified` | schema/fixture/接口边界一致 | 实时链路或硬件可用 |
| `host_verified` | 独立 host 路径通过 | ESP32、网络或声学通过 |
| `build_passed` | 当前源码/工具链编译链接成功 | 已烧录或启动 |
| `image_sized` | 固件相对 partition 余量已读取 | runtime heap/stack 足够 |
| `boot_observed` | 指定 artifact 在指定板启动 | 未观察功能正确 |
| `device_verified` | 指定端云行为在记录环境通过 | 其他网络/版本/长稳通过 |
| `acoustic_verified` | 固定声学条件和指标通过 | 任意房间、距离或噪声均通过 |
| `public_path_verified` | 指定公网路径通过 | 所有 NAT/运营商环境通过 |
| `measured` | 指标有原始数据、环境和统计 | 可外推到其他机器 |
| `not_run` | 未执行并说明原因 | 通过或失败 |

## 2. 测试层级

### Repository

- 禁止路径、secret、generated binary、external checkout、Direct/AIMP 资产。
- source/license/dependency lock 完整性。
- protocol registry、schema、fixture 与文档链接一致。
- architecture dependency 方向和 package import 边界。

### Unit

- Grant encode/verify、expiry、wrong worker/device、duplicate/replay。
- Worker selection、capacity、heartbeat TTL、drain、lease/fencing。
- JSON duplicate/unknown/oversize/state parsing。
- UDP header/nonce/AAD/replay/reorder/generation。
- queue overflow、timeout、double close、cancellation 和 provider error mapping。

### Contract

- `protocol/` positive/negative fixture 被 Python/C++ 直接消费。
- Director HTTP/Worker heartbeat/bootstrap schema。
- Provider fake server 的 streaming、metadata、timeout、429/5xx 和 malformed response。
- Firmware overlay source/lifecycle/config/secret contract。

### Integration

- In-process Director + memory store 仅验证确定性控制语义。
- Redis-compatible store 验证 atomic lease、fencing、TTL、single-use grant 和多 Director race。
- Worker WSS/UDP + deterministic Agent runner 验证 media/control/abort/close，无真实 provider 波动。
- Roomless LiveKit Agent + fake providers 验证 VAD/turn/TTS generation。

### Host E2E

- 独立进程 Director、Worker、Redis 和 reference client。
- WSS 与 UDP 分 profile，包含 restart、drain、overload、wrong route 和 fresh bootstrap。
- 使用固定 PCM/Opus，不双发真实用户麦克风。

### Real provider

- 显式启用的 FunASR、LLM、TTS smoke；记录 provider/model/region/config 和 redacted request id。
- 不进入默认 CI，不输出 API key、原始 provider body 或用户音频。
- 旧 provider 闭环只作 baseline；新仓组合必须重跑。

### Firmware build/HIL

- 固定 ESP-IDF、target、source revision、overlay digest、sdkconfig input digest。
- clean build、size、artifact SHA-256。
- 指定 COM/board 授权后才 flash；记录 flash 命令、boot log 和 artifact identity。
- WSS/UDP、UI/NVS、AEC、字幕、打断、音量、网络切换、Server restart 分项验收。

## 3. 风险专项

### 弱网

冻结 codec/provider/设备距离，在同一网络注入矩阵比较 WSS/UDP：0/1/3/5% random loss、burst loss、jitter、
reorder 和带宽限制。采集 p50/p95/p99 speech-end-to-playout、media age、late/loss/PLC、underrun、stop tail。
实验参数和通过门限在首次 baseline 后写入固定实验，不在本文拍脑袋给数字。

### 声学

固定设备朝向、扬声器音量、麦克风距离、房间、背景噪声和测试话术。覆盖静音、近讲、远讲、TTS 播放中
double-talk 和连续 20 轮。采集误唤醒、漏检、ASR CER/人工转写、回声自激和 interrupt tail。

### 稳定与容量

- 单设备 30 分钟：heap/stack high-water、queue、socket、provider client、underrun 和 reconnect。
- 并发阶梯：1/5/10/...，但不得假设默认 `5` 已测量。
- cancellation storm、provider 429/timeout、Worker drain/crash、Redis 短时故障。
- 通过门限由目标部署机器的 SLO 冻结。

### 安全

- Secret scan、依赖/许可证、malformed fuzz、oversize、rate limit。
- Grant tamper/expiry/replay/wrong audience/fencing。
- UDP auth failure、source spoof、replay、large jump、nonce/sequence exhaustion、amplification。
- 日志抽样确认无 token/key/Wi-Fi password/raw audio/provider body。

## 4. Release gate

1. Ruff/pytest/repository/contract 全部通过，无 skipped 被写成 passed。
2. Director memory 与 Redis adapter 测试分开，生产水平扩展必须有 Redis 证据。
3. Firmware clean build/size 和 source contract 通过。
4. 最终 artifact WSS baseline 完成真机 real-provider 闭环。
5. UDP 只有通过 HIL、安全、弱网和长稳才可进入 `auto`；否则保持显式 challenger。
6. 20 轮、30 分钟、网络切换、Server restart 和 AEC/double-talk 完成或有批准 waiver。
7. 需求追踪无未解释缺口，rollback artifact 与 drain 步骤已验证。

## 5. 当前缺口

当前 Redis-enabled pytest `179 passed`，已经覆盖 shared grant consumption、atomic lease/fencing、Redis-backed
重建与跨实例语义；它不等于真实多进程容量、Redis HA 或网络分区演练。Lifecycle repair `d2fa0ca` 的 launcher
stop/start 已实测通过。Host synthetic Chinese 已完成真实 provider media E2E，但只是一组 host 单次观测。

Final reference firmware 已完整烧录并达到 `boot_observed`；设备唤醒、WSS handshake、UDP probe 与 600+ UDP Opus
uplink packets 已观察。因采集 peak 偏低未触发真机 ASR。UI/触摸人工验收、真机 ASR/TTS、弱网、声学、20 轮、
30 分钟和容量仍为 `not_run`。任何当前测试报告必须追加到 evidence artifact，而不是直接改写本策略中的证据词义。
