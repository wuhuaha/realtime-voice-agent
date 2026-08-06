# 测试策略

状态：accepted
更新日期：2026-07-30

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

- 禁止路径、secret、generated binary、external checkout 和未登记媒体协议资产。
- source/license/dependency lock 完整性。
- protocol registry、schema、fixture 与文档链接一致。
- architecture dependency 方向和 package import 边界。

### Unit

- Grant encode/verify、expiry、wrong worker/device、duplicate/replay。
- Worker selection、capacity、heartbeat TTL、drain、lease/fencing。
- Device exact release 的认证、重复/stale fence 幂等，以及 lease identity 有效但 media 字段无效时仍可释放 lease。
- JSON duplicate/unknown/oversize/state parsing。
- UDP header/nonce/AAD/replay/reorder/generation。
- UDP 双向 authenticated cursor resync、replay state 保留、forward-jump bound、live-edge queue replacement 和
  playout quality epoch。
- playback final drain deadline、exact-final 单包 PLC、degraded/failed、Server exact-target terminal watchdog，以及
  Endpoint `session.opened` deadline。
- Roomless segment task failure -> runner terminal -> RVA failure 的传播，且 failure 后不发送虚假 `response.end`。
- queue overflow、timeout、double close、cancellation 和 provider error mapping。
- Redis connect/command timeout、Worker 10 秒总关停预算/有界 release heartbeat、TTS queue timeout 与 MiMo SSE
  line/event/data-line/audio-chunk/total-response 上限。

### Contract

- `protocol/` positive/negative fixture 被 Python/C++ 直接消费。
- Director HTTP/Worker heartbeat/bootstrap schema。
- Provider fake server 的 streaming、metadata、timeout、429/5xx 和 malformed response。
- 默认 [native target](../../firmware/apps/voice_terminal/README.md) 验证 composition/source/config/secret contract；artifact identity
  绑定 source revision、ESP-IDF/target、sdkconfig input、partition table、font/generated asset 与 component lock digest。

### Integration

- In-process Director + memory store 仅验证确定性控制语义。
- Redis-compatible store 验证 atomic lease、fencing、TTL、single-use grant 和多 Director race。
- Worker WSS/UDP + deterministic Agent runner 验证 media/control/abort/close，无真实 provider 波动。
- Roomless LiveKit Agent + fake providers 验证 VAD/turn/TTS generation。

### Host E2E

- 独立进程 Director、Worker 和 Python Desktop Reference Client；需要多实例语义时另启 Redis。
- 默认确定性测试逐 profile 覆盖 bootstrap、single-use grant、session.open、transcript、response、下行媒体、
  playback.started/ended、close；WSS 与 UDP 分开断言 sequence、timestamp、generation 和认证边界。
- 使用内存生成的固定 PCM 和锁定的 Opus adapter，不提交音频 fixture，不访问真实 provider 或声卡。
- restart、drain、overload、wrong route 和 fresh bootstrap 属于扩展 host 场景，不得因单轮 happy path 通过而隐含声称。
- `interactive` 麦克风/扬声器只作为显式 host smoke；其结果记录设备、采样率、backend 和环境，默认 CI 不运行。
- SoundDevice 的 render fact 只证明 PortAudio 时钟已跨过报告的 output-latency 预计边界；未做真实 DAC/声学采集时，
  不得升级为 physical/acoustic verified。

### Real provider

- 显式启用的 FunASR、LLM、TTS smoke；记录 provider/model/region/config 和 redacted request id。
- 不进入默认 CI，不输出 API key、原始 provider body 或用户音频。

### Firmware build/HIL

- 默认 native target 固定 ESP-IDF、target、source revision、sdkconfig input、partition table、font/generated asset 与
  component lock identity；native artifact 不以 compatibility overlay digest 标识。
- clean build、size、artifact SHA-256。
- 确认目标设备与串口并获得授权后才 flash；记录 flash 命令、boot log 和 artifact identity。
- WSS/UDP、UI/NVS、AEC、字幕、打断、音量、网络切换、Server restart 分项验收。
- 注入 final media 丢包、cursor resync 和 playback terminal 丢失，核对 UI 能回到可恢复状态、Endpoint 不虚报
  `completed`、Server 在配置 deadline 后 fresh close。
- ESP-SR `model` 分区存在/缺失各启动一次；缺失时验证只禁用神经降噪，并重新执行 AEC/VAD、播放中
  double-talk、上行 PCM 与 ASR 项。代码路径或 build 通过不能替代这组声学证据。

## 3. 风险专项

### 弱网

冻结 codec/provider/设备距离，在同一网络注入矩阵比较 WSS/UDP：0/1/3/5% random loss、burst loss、jitter、
reorder 和带宽限制。采集 p50/p95/p99 speech-end-to-playout、media age、late/loss/PLC、underrun、stop tail。
实验参数和通过门限在首次 baseline 后写入固定实验，不在本文拍脑袋给数字。

Product的Linux netns harness先提供`isolated_loopback_netns` completion matrix：按Worker media port应用netem，
同一scenario/repeat只保证逻辑`pair_id`相同。只有`tc`接受seed且raw/aggregate记录`tc_seed_control=applied`和
`paired_randomness=true`时，才能声称WSS/UDP共享受控随机序列；fallback无seed时必须记录
`tc_seed_control=unavailable`、`paired_randomness=false`和降级后的`comparison_limit`，不得把逻辑pair解释为随机
扰动配对。用独立TCP `30:`/UDP `40:` handle及profile-specific counter验证规则命中，检查严格媒体闭环和最终
`active_sessions=0`。clean或不适用profile不要求impairment counter；loss/reorder规则已命中但有限样本没有实际
drop/reorder时必须记录`no_impairment_observed`，不能写成已观察到扰动。route语义必须区分
“第一条lease由二次reacquire证明已释放”和“最终lease只收到release request accepted”。仅有session completion
latency和qdisc/filter counters时必须标记`completion_only`；没有media age、late/loss/PLC与物理播放测量，不得升级
为transport性能比较。

真实Linux执行若在seed capability、namespace/qdisc或TIME_WAIT端口回收探针等前置条件失败，矩阵状态保持
failed prerequisite / `not_run`。前置问题修复后可把实际通过的代表cell登记为`host_verified/completion_only`，但
1-repeat代表矩阵不能继承为完整scenario/repeats、性能比较或release gate通过；每个证据声明必须列出实际scenario、
profile、repeat和aggregate capability字段。

完整矩阵必须显式覆盖首个媒体包、帧间媒体包和最终媒体包丢失。loss场景只接受两类结果：严格完整的`completed`；
或`bounded_recovery_verified`，且后者必须同时证明实际发生drop、Endpoint上报`playback.ended=stopped`、fresh session
identity变化、旧generation媒体未恢复、Worker `active_sessions=0`、route可重新获取以及进程/端口回收。non-loss场景
不得用bounded recovery替代完整成功；首包丢失后只播放剩余内容却上报`completed`也必须失败。任一cell失败时aggregate
不得继承旧小矩阵的passed状态，修复后必须完整fresh rerun。

### 声学

固定设备朝向、扬声器音量、麦克风距离、房间、背景噪声和测试话术。覆盖静音、近讲、远讲、TTS 播放中
double-talk 和连续 20 轮。采集误唤醒、漏检、ASR CER/人工转写、回声自激和 interrupt tail。

### 稳定与容量

- 单设备 30 分钟：heap/stack high-water、queue、socket、provider client、underrun 和 reconnect。
- 并发阶梯：1/5/10/...，但不得假设默认 `5` 已测量。
- Host capacity/churn的session必须同时满足非零上行、预期下行帧数、唯一完整playback、必需事件、Worker active
  归零和进程/端口回收；Desktop client正常返回本身不能算成功。第一条route释放可由二次reacquire证明，最终测试
  lease只验证release request accepted。Worker drain只在实际启动至少两个Worker时形成证据。
- 重复短session的30分钟/2小时churn与设备连续operation soak是不同证据，执行状态必须分别记录。UDP短期key/grant
  按协议freshness deadline主动换新，因此continuous-operation按“用户体验连续、旧route释放、fresh identity、旧媒体隔离、
  新probe认证和资源有界”验收，不要求单个加密session跨越expiry永久存活；runner未实现时使用
  `not_run/not_implemented`，不能只写笼统的长稳未测。
- 30分钟和2小时结果按profile分别登记；一个profile通过不代表另一profile或双profile release gate通过。
- cancellation storm、provider 429/timeout、Worker drain/crash、Redis 短时故障。
- bootstrap 200 后本地 WSS/task allocation 失败、exact release、立即 fresh bootstrap；以及 WSS teardown 无法确认时
  fail-closed restart，不允许旧 owner 与新 session 并存。
- 通过门限由目标部署机器的 SLO 冻结。

### 安全

- Secret scan、依赖/许可证、malformed fuzz、oversize、rate limit。
- Grant tamper/expiry/replay/wrong audience/fencing。
- UDP auth failure、source spoof、replay、large jump、nonce/sequence exhaustion、amplification。
- 日志抽样确认无 token/key/Wi-Fi password/raw audio/provider body。

## 4. Release gate

1. Ruff/pytest/repository/contract 全部通过，无 skipped 被写成 passed。
2. Director memory 与 Redis adapter 测试分开，生产水平扩展必须有 Redis 证据。
3. 默认 native target 的 clean build/size、composition/source/config/secret contract 与完整 artifact identity 通过。
4. 最终 artifact WSS baseline 完成真机 real-provider 闭环。
5. UDP 只有通过 HIL、安全、弱网和长稳才可进入 `auto`；否则保持显式 challenger。
6. 20 轮、30 分钟、网络切换、Server restart 和 AEC/double-talk 完成或有批准 waiver。
7. 需求追踪无未解释缺口，rollback artifact 与 drain 步骤已验证。

## 5. 当前状态

本策略只定义证据口径和发布门禁，不记录单次 commit、端口、地址、hash、日志或实验编年。当前源码
的通过项、显式 skip 与缺口统一维护在 [Release readiness](release-readiness.md)；原始日志、音频、指标和大型
artifact 保存在受控证据系统中，并以 source identity 关联。
