# Release readiness

更新日期：2026-08-03
状态：WSS baseline device verified / alpha release work remains

本文只记录当前 Product 候选和可复核的发布门禁。历史 artifact、临时地址、SSID、原始串口日志和旧协议结论不构成
当前 release evidence；未执行的门禁保持 `not_run` 或 `incomplete`。

## 当前候选身份

- 分支：`codex/lifecycle-convergence-hardening`
- 历史真机/公网 artifact source：`ae56facccf11b99c536e5c5bc93c3e4d41602028`
- 历史真机 app SHA-256：`75c5d4f8a16cdb679ad4c5bb902acdd8bb42395d81cc0f4d76455d784a4a2dd4`
- 当前 freshness 修复 source：`c1dc5bbdcbb7c35f65418a2d3b39cb4cc29c3125`
- 当前 private deployment app SHA-256：`afa03afefb247b728b2477388834f9470a7002727465df47f7caab928f03441d`
- 当前 private sdkconfig SHA-256：`960d1a7e41bf0604a827e0e5430195b2b9962b2a20b6aef6599a0965c5be0557`
- 当前 Server release：`rva-20260803T065329Z-c1dc5bb`
- 当前 Server archive SHA-256：`162fb7a6b1558686733f2cd5b804e97383edcc6a4d6417f2283cd52632c87965`
- 当前 WSS HIL：`device_verified`
- 当前 public release bundle：`not_run`

`ae56fac` 是最后一个具有完整真机和公网日志的历史 identity。它的 UDP gate 完整通过；WSS 虽多次完成 ASR、LLM、
TTS 和物理播放，承载播放的两次 session 随后均以 `1013/media_overloaded` 关闭，因此 WSS final gate 未通过。当前
修复同时修改 Server media timeline 与 Firmware WSS sender，历史 device evidence 不得外推到修复版。当前修复已从
clean `c1dc5bb` 和 ignored/private Kconfig input 构建 deployment image；private input、凭据和 image
不进入 Git 或公开 release。正式 tag 前仍须从最终 source fresh 构建无凭据 public bundle；本文之后的 docs-only
evidence commit不改变 Server/Firmware source identity。

## 软件与构建门禁

历史 GitHub Actions 已覆盖 `repository`、`server`、`desktop-reference`、Linux host E2E、Redis integration、native
host contracts 和 ESP-IDF build/size。当前修复的 focused Server tests 为 `41 passed`，完整 Server tests 为
`298 passed, 3 skipped`；Server Ruff、root Ruff、root `52 passed`、Desktop Reference `116 passed, 4 deselected`、
repository verifier、native runtime host tests 与 `git diff --check` 均通过。3 个 Server skip 是未配置 Redis subprocess
URL，4 个 Desktop deselect 是 Linux-only deterministic host E2E；二者已有历史 Linux/CI 证据，但当前
commit `c1dc5bb` 的 GitHub Actions [`ci` run 30791336769](https://github.com/wuhuaha/realtime-voice-agent/actions/runs/30791336769)
7 个 job 全部成功。Private deployment app 从 clean source/config构建，大小 `0x21aa80`，4 MiB app partition余量
`0x1e5580`（47%）；其 provenance、source revision、config digest与 artifact digest 已记录。该 private app不等于
公开 release bundle。

## 自动稳定性与故障注入

Linux 隔离 checkout 在 `fe39a6c` 上连续运行 1804 秒 deterministic churn，共 211 轮；每轮重新建立和回收独立
Director/Worker，并执行 WSS low-level、WSS DesktopApp、UDP low-level 和 UDP DesktopApp 四个 case。WSS
`422/422`、UDP `422/422` 通过，失败为 0，最长单轮 8951 ms，最终无残留进程。`d840036` 未修改被测 production
runtime，因此该证据只继承到 `d840036`。当前 freshness 修复改变 Server media timeline 和 Firmware WSS sender，
1804 秒 churn 尚未在当前 source 重跑，只能作为历史对照。

另执行 deterministic fault matrix：Server `50/50`、Desktop `63/63` 通过，覆盖 UDP authentication、replay、
gap/deadline、refresh、PLC、fresh reopen、generation fence，以及 WSS teardown、playback terminal、cancel 和 cleanup。
这些结果是历史 `host_verified` 协议/生命周期证据；当前修复新增的 focused/full regression 已通过，但未重跑完整
fault matrix。它们也不是物理网卡上的 random loss、burst、连续 jitter、带宽限制或公网 TLS 测量；当前门禁保持
`not_run/incomplete`，UDP 继续为显式 opt-in。

## Linux 公网部署

当前只读 release 为 `rva-20260803T065329Z-c1dc5bb`，Worker incarnation 为
`worker-ol-rva-20260803T065329Z-c1dc5bb`。Director 和 Worker 均由 Linux `systemd --user` 运行；最终观测结果：

- Director：`ready`，coordination=`redis`
- Worker：`ready`，`draining=false`，`healthy=true`，`active_sessions=0/5`
- provider network、coordination、RVA WSS 和 RVA UDP socket：ready
- advertised profiles：`wss-opus/1`、`udp-opus-gcm/1`

Release archive strict check通过，mode `555`；旧 `ae56fac` release保留。后续 replacement必须分配唯一
`worker_id`，不得用重复 identity原地 restart。

同一部署上执行确定性 bootstrap smoke：WSS-only 与 WSS+UDP 两种请求均命中上述 Worker，返回预期 profiles，
随后 exact route release 成功。该 smoke 证明公网 admission/bootstrap，不替代真机 UDP media。

历史已登记部署曾使用 Desktop Reference Client、服务器 loopback WSS 和本机离线生成的中文 PCM 执行一次
real-provider canary：119 个上行 frame、137 个下行 playback frame、1 次完整 playback fact；FunASR final、DeepSeek LLM、
MiMo TTS、normal close 和 exact release 均完成，最终 `active_sessions=0`。`turn_latency_summary` 为
`status=complete`，TTS 与 playback facts 均归属 `turn-000001`，`endpoint_playback_finished` 为
`interrupted=false`、`playback_position_ms=8220`；未观察到 `ReadTimeout`、playback terminal timeout 或异常关闭。
该 canary 不保存转写、回复或下行音频，也不替代 ESP32 DAC、声学或 UDP HIL。

本部署使用实验室 HTTP/WS 公网入口，不构成生产 TLS 门禁。正式环境仍必须提供受信 HTTPS/WSS 域名、证书校验、
入口限流和受控 secret；不得把本次公网可达性提升为 production security 通过。

## ESP32-S3 真机回归

目标为立创实战派 ESP32-S3 revision 0.2。当前 `c1dc5bb` deployment app只烧录 `0x10000` app partition，
esptool hash verified且未擦 NVS；启动日志确认 app version、显示/触摸、Qwen 字体、Wi-Fi、WakeNet、
AEC `VOIP_LOW_COST`、WebRTC VAD、双通道 AFE 和 codec均正常，无 panic、Task WDT或 reboot loop。

当前修复版 WSS 真机门禁：

- selected profile=`wss-opus/1`，bootstrap `200`
- 上行 `442` 个 packet、解码/runner `1326` 个 PCM frame，`invalid_opus_packets=0`
- FunASR final、DeepSeek-compatible LLM 与 MiMo TTS完整运行；下行 `73` 个 packet
- Endpoint上报 `playback_position_ms=6570`、`interrupted=false`，用户确认完整符合预期
- `close_code=1000`、`close_reason=normal`、`overload_source=none`、`overload_dropped_packets=0`
- `session_closed reason=user_initiated`；Director exact release `200`，最终 `active_sessions=0/5`
- Director/Worker `NRestarts=0`，无 media overload、traceback、terminal timeout或残留 route

作为回归来源，历史 artifact `ae56fac` 的 app SHA-256 为
`75c5d4f8a16cdb679ad4c5bb902acdd8bb42395d81cc0f4d76455d784a4a2dd4`；在 `COM11` 只烧 app partition，
esptool 报告 hash verified，未擦 NVS。

deployment image 启动后完成已配置 Wi-Fi 与 Director endpoint 解析；显示、触摸、Qwen 字体、WakeNet、
AEC `VOIP_LOW_COST`、WebRTC VAD 和双通道 AFE 均启动。当前实验 endpoint 为 HTTP，因此 SNTP 失败不会阻断
bootstrap；UDP 本地轮换使用 authenticated `refresh_after_ms` 的 monotonic deadline，Server 继续执行绝对 expiry。
设备通过 `Hi ESP` 分别建立真实 WSS 和 UDP 会话。`ae56fac` 的 WSS 多次完成 ASR、LLM、MiMo TTS 与
`3690/5850/7740 ms` 等完整播放事实，但承载播放的两次 session 随后分别以 media age `825/1153 ms` 的
`1013/media_overloaded` 关闭；两次均为 `qsize=0`、`dropped_packets=1`、`fresh_packet_available=false`。第三次
WSS 正常关闭但没有下行播放，因此历史 WSS final gate 未通过。UDP 回归记录：

- bootstrap `200`、selected profile=`udp-opus-gcm/1`
- UDP socket 建立后 authenticated probe 单次成功，`elapsed_ms=26`，Server 完成 source pinning
- 用户确认真实问答完整流畅；Server 收到 `3870/10620 ms` 等自然完成的 playback fact
- 关闭前上行 `1649` 个 UDP packet、`4947` 个 decoded PCM frame，`invalid_opus_packets=0`
- 下行 `492` 个 packet；authenticated/source pinned/probe ack 均为 `1`，invalid=`0`
- MIC stop 后 `close_code=1000`、`close_reason=normal`、`session_closed reason=user_initiated`
- Director exact release `200`；最终 Worker `active_sessions=0`

`ae56fac` UDP session 最终 `1000/normal`、`session_closed reason=user_initiated`、exact release `200`，Worker 回到
`active_sessions=0`。测试环境持续存在背景人声；NS、VAD
切分和 ASR 准确率不属于本项目本轮门禁，只要求这些输入不得破坏 transport、session、playback generation、terminal
或资源释放。声学、真实 netem 和固定延迟分位数不进入本次门禁。

## 当前发布门禁

| Gate | 当前状态 | 当前证据与完成条件 |
| --- | --- | --- |
| Product commit + CI | `host_verified` | `c1dc5bb` 已 push，CI 7/7通过 |
| Server immutable archive | `public_path_verified` | `c1dc5bb` 只读 archive strict check、部署和 rollback保留通过 |
| Linux Director/Worker readiness | `public_path_verified` | 当前 release ready，profiles、capacity、provider、coordination和 UDP socket正常 |
| Native clean build + size | `build_passed / image_sized` | `c1dc5bb` private app provenance/digest，47% app余量；public bundle仍未构建 |
| Flash/boot/display/touch | `device_verified` | 当前 app hash verified；启动、显示、触摸、字体、AFE无异常 |
| Wi-Fi/NVS/bootstrap | `device_verified` | 保留 NVS，自动联网并完成当前公网 bootstrap |
| WSS voice loop | `device_verified` | 当前 source完成6570 ms播放、normal close/release、0 overload |
| UDP admission/bootstrap | `not_run` | 历史 `ae56fac` device verified；修复版未运行 |
| UDP voice loop | `not_run` | 历史 `ae56fac` 双向 Opus、完整 playback fact、0 invalid；修复版未运行 |
| End-to-end latency | `not_run` | alpha known limitation；未承诺固定 p50/p95/p99 SLO |
| Weak network | `not_run` | 历史 deterministic fault matrix通过；当前 source与真实 random/burst/jitter/netem均未测 |
| Stability | `incomplete` | 历史 1804 秒/211轮通过；当前 source完成自动回归和短 HIL，未重跑长 soak |
| AEC/acoustic | `out_of_scope` | 当前开源定位不以 NS/ASR/AEC 主观效果为 release gate |
| Security/repository | `host_verified / production incomplete` | 当前 secret/repository scan通过；历史 SBOM/许可证digest不替代当前 release SBOM，后者仍`not_run`；TLS/限流由部署方提供 |

## 证据规则

- `host_verified`、`public_path_verified`、`build_passed` 和 `device_verified` 不得互相替代。
- 设备结论必须绑定完整 Product commit、firmware digest 和 private config digest；不公开 private input 或 binary。
- skipped、旧 artifact 或旧日志不计为当前通过。
- 正式 tag/release 前仍需从目标 commit fresh build，并完成 release scope 中未关闭的门禁。
