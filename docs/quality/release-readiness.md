# Release readiness

更新日期：2026-08-04
状态：WSS/UDP device verified / alpha release work remains

本文只记录当前 Product 候选和可复核的发布门禁。历史 artifact、临时地址、SSID、原始串口日志和旧协议结论不构成
当前 release evidence；未执行的门禁保持 `not_run` 或 `incomplete`。

## 当前候选身份

- 分支：`main`
- 当前 Server source：`3f207a51f42c2a7d53982a5ab9b3117795549f62`
- 当前 Firmware source：`c1dc5bbdcbb7c35f65418a2d3b39cb4cc29c3125`
- 当前 private deployment app SHA-256：`afa03afefb247b728b2477388834f9470a7002727465df47f7caab928f03441d`
- 当前 private sdkconfig SHA-256：`960d1a7e41bf0604a827e0e5430195b2b9962b2a20b6aef6599a0965c5be0557`
- 当前 Server release：`rva-20260804T034936Z-3f207a5`
- 当前 Server archive SHA-256：`24341a1a94e4a993900a3accfc899b6039e9d4b6f63f96568acd0deb46b07642`
- 当前 WSS HIL：`device_verified`
- 当前 UDP HIL：`device_verified`
- 当前 public release bundle：`not_run`

当前 HIL 使用 `c1dc5bb` Firmware 与 `3f207a5` Server。Server 的 bounded WSS catch-up 只处理尚未 decode 的孤立
stale packet；catch-up 不收敛、partial runner push、UDP stale 和真实 backpressure 仍 fail closed。Firmware 由 clean
source 和 ignored/private Kconfig input 构建；private input、凭据和 image 不进入 Git 或公开 release。正式 tag 前仍须
从最终 source fresh 构建无凭据 public bundle；本文之后的 evidence-only commit 不改变 Server/Firmware source identity。

## 软件与构建门禁

历史 GitHub Actions 已覆盖 `repository`、`server`、`desktop-reference`、Linux host E2E、Redis integration、native
host contracts 和 ESP-IDF build/size。当前 `3f207a5` 本地完整门禁为 root `52 passed`、Server
`310 passed, 3 skipped`、Desktop Reference `116 passed, 4 deselected`；Ruff、repository contracts、tracked/untracked
secret scan 与 `git diff --check` 均通过。3 个 Server skip 是未配置 Redis subprocess URL，4 个 Desktop deselect 是
Linux-only deterministic host E2E；当前 commit 的远端 CI 状态未在本轮登记。Private deployment app 从 clean
source/config构建，大小 `0x21aa80`，4 MiB app partition余量
`0x1e5580`（47%）；其 provenance、source revision、config digest与 artifact digest 已记录。该 private app不等于
公开 release bundle。

## 自动稳定性与故障注入

Linux 隔离 checkout 在 `fe39a6c` 上连续运行 1804 秒 deterministic churn，共 211 轮；每轮重新建立和回收独立
Director/Worker，并执行 WSS low-level、WSS DesktopApp、UDP low-level 和 UDP DesktopApp 四个 case。WSS
`422/422`、UDP `422/422` 通过，失败为 0，最长单轮 8951 ms，最终无残留进程。`d840036` 未修改被测 production
runtime，因此该证据只继承到 `d840036`。当前 freshness 修复改变 Server media timeline 和 Firmware WSS sender，
1804 秒 churn 尚未在当前 source 重跑，只能作为历史对照。

当前 `826cd66` 重新执行 deterministic fault matrix：Server `54/54`、Desktop `59/59` 通过，覆盖 UDP
authentication、replay、gap/deadline、refresh、PLC、fresh reopen、generation fence，以及 WSS teardown、playback
terminal、cancel 和 cleanup。另运行 `618.982s` 当前 source短 churn，共 20 个完整轮次、80 个 case：WSS
low-level `20/20`、UDP low-level `20/20`、UDP DesktopApp `20/20`、WSS DesktopApp `19/20`。唯一失败发生在第 2 轮
Worker readiness 15 秒超时，未进入 session/media；随后连续 73 个 case通过，最终无残留 RVA进程或监听端口。
同一四路径在当前 Linux CI为 `4/4`通过；该失败仅观察为 Windows host harness readiness超时，尚未复现或定因，
且未进入 session/media，因此不作为媒体或协议的通过/失败证据。
这些结果不是物理网卡上的 random loss、burst、连续 jitter、带宽限制、公网 TLS或长时间设备 soak；当前门禁仍保持
`not_run/incomplete`，UDP 继续为显式 opt-in。

## Linux 公网部署

当前只读 release 为 `rva-20260804T034936Z-3f207a5`，Worker incarnation 为
`worker-ol-rva-20260804T034936Z-3f207a5`。Director 和 Worker 均由 Linux `systemd --user` 运行；最终观测结果：

- Director：`ready`，coordination=`redis`
- Worker：`ready`，`draining=false`，`healthy=true`，`active_sessions=0/5`
- provider network、coordination、RVA WSS 和 RVA UDP socket：ready
- advertised profiles：`wss-opus/1`、`udp-opus-gcm/1`

Release archive strict check通过，release tree只读，上一 release保留。后续 replacement必须分配唯一
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
- 最终回归 session 上行 `1888` 个 packet、解码/runner `5664` 个 PCM frame，`invalid_opus_packets=0`
- FunASR final、DeepSeek-compatible LLM 与 MiMo TTS 多轮完整运行；下行 `263` 个 packet
- `close_code=1000`、`close_reason=normal`、`overload_source=none`、`overload_dropped_packets=0`
- `session_closed reason=user_initiated`；Director exact release `200`，最终 `active_sessions=0/5`
- Director/Worker `NRestarts=0`，无 media overload、freshness/catch-up异常、traceback或残留 route

回归开始时移动设备导致 CH340K `COM14` 物理断连：测试前端口存在，断连后 Windows 只剩原生 USB `COM11`；设备
没有整机重启，WSS 上行仍继续。受干扰的首个 session 缺失 `playback.ended` 并由 Server watchdog 以
`1011/playback_terminal_timeout` 关闭，作为污染样本排除，不计为通过或产品根因。设备随后自动 fresh reconnect；上述
最终 session 在 COM14 已断开的条件下完成正常多轮和 exact close。该观察不证明 USB 断连与 terminal fact 丢失存在
因果关系，后续若无物理干扰仍复现，须单独进入 endpoint terminal-fact 诊断。

deployment image 启动后完成已配置 Wi-Fi 与 Director endpoint 解析；显示、触摸、Qwen 字体、WakeNet、
AEC `VOIP_LOW_COST`、WebRTC VAD 和双通道 AFE 均启动。当前实验 endpoint 为 HTTP，因此 SNTP 失败不会阻断
bootstrap；UDP 本地轮换使用 authenticated `refresh_after_ms` 的 monotonic deadline，Server 继续执行绝对 expiry。
设备通过 `Hi ESP` 分别建立真实 WSS 和 UDP 会话。当前 UDP 回归记录：

- bootstrap `200`、selected profile=`udp-opus-gcm/1`
- UDP socket 建立后 authenticated probe 单次成功，`elapsed_ms=28`，Server 完成 source pinning
- 用户确认真实问答完整流畅；Endpoint 上报 `playback_position_ms=24300`、`interrupted=false`
- 关闭前上行 `573` 个 UDP packet、`1719` 个 decoded/runner PCM frame，`invalid_opus_packets=0`
- 下行 `270` 个 packet；authenticated/source pinned/probe ack 均为 `1`，invalid=`0`
- MIC stop 后 `close_code=1000`、`close_reason=normal`、`session_closed reason=user_initiated`
- Director exact release `200`；最终 Worker `active_sessions=0`

测试环境持续存在背景人声；NS、VAD
切分和 ASR 准确率不属于本项目本轮门禁，只要求这些输入不得破坏 transport、session、playback generation、terminal
或资源释放。声学、真实 netem 和固定延迟分位数不进入本次门禁。

## 当前发布门禁

| Gate | 当前状态 | 当前证据与完成条件 |
| --- | --- | --- |
| Product commit + CI | `host_verified` | `3f207a5` 已 push；本地完整门禁通过，当前远端 CI状态未登记 |
| Server immutable archive | `public_path_verified` | `3f207a5` 只读 archive strict check、部署和 rollback保留通过 |
| Linux Director/Worker readiness | `public_path_verified` | 当前 release ready，profiles、capacity、provider、coordination和 UDP socket正常 |
| Native clean build + size | `build_passed / image_sized` | `c1dc5bb` private app provenance/digest，47% app余量；public bundle仍未构建 |
| Flash/boot/display/touch | `device_verified` | 当前 app hash verified；启动、显示、触摸、字体、AFE无异常 |
| Wi-Fi/NVS/bootstrap | `device_verified` | 保留 NVS，自动联网并完成当前公网 bootstrap |
| WSS voice loop | `device_verified` | 当前 Server/Firmware 完成多轮、1888/263包、normal close/release、0 overload |
| UDP admission/bootstrap | `device_verified` | 当前 Server/Firmware 完成 AEAD probe、source pinning，elapsed 28 ms、invalid 0 |
| UDP voice loop | `device_verified` | 双向 Opus、24300 ms完整 playback fact、573/270包、normal close、0 invalid |
| End-to-end latency | `not_run` | alpha known limitation；未承诺固定 p50/p95/p99 SLO |
| Weak network | `not_run` | 当前 deterministic fault matrix通过；真实 random/burst/jitter/netem仍未测 |
| Stability | `incomplete` | 当前 source完成 fault matrix、618.982秒短 churn和短 HIL；一次 Windows harness readiness超时尚未定因，且未重跑长 soak |
| AEC/acoustic | `out_of_scope` | 当前开源定位不以 NS/ASR/AEC 主观效果为 release gate |
| Security/repository | `host_verified / production incomplete` | 当前 secret/repository scan通过；历史 SBOM/许可证digest不替代当前 release SBOM，后者仍`not_run`；TLS/限流由部署方提供 |

## 证据规则

- `host_verified`、`public_path_verified`、`build_passed` 和 `device_verified` 不得互相替代。
- 设备结论必须绑定完整 Product commit、firmware digest 和 private config digest；不公开 private input 或 binary。
- skipped、旧 artifact 或旧日志不计为当前通过。
- 正式 tag/release 前仍需从目标 commit fresh build，并完成 release scope 中未关闭的门禁。
