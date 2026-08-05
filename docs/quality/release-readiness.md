# Release readiness

更新日期：2026-08-05
状态：当前候选 host/弱网/30m 已验证 / fresh bundle 与 HIL 尚未执行

本文只记录当前 Product 候选和可复核的发布门禁。历史 artifact、临时地址、SSID、原始串口日志和旧协议结论不构成
当前 release evidence；未执行的门禁保持 `not_run` 或 `incomplete`。

## 上一可恢复发布基线

- 分支：`main`
- 当前 Server source：`3f207a51f42c2a7d53982a5ab9b3117795549f62`
- 当前 Firmware source：`c1dc5bbdcbb7c35f65418a2d3b39cb4cc29c3125`
- 当前 private deployment app SHA-256：`afa03afefb247b728b2477388834f9470a7002727465df47f7caab928f03441d`
- 当前 private sdkconfig SHA-256：`960d1a7e41bf0604a827e0e5430195b2b9962b2a20b6aef6599a0965c5be0557`
- 当前 Server release：`rva-20260804T034936Z-3f207a5`
- 当前 Server archive SHA-256：`24341a1a94e4a993900a3accfc899b6039e9d4b6f63f96568acd0deb46b07642`
- WSS HIL：`device_verified`
- UDP HIL：`device_verified`
- 当前 public release bundle source：`6fddf82654460ae1bf2a3244cbca8d5bceac41d6`
- 当前 public release bundle SHA-256：`e63d799cce524bab65b45a582592776f88c92072dbea2ce45ae1ad1865c6f3be`
- 当前 public release app SHA-256：`316a1aeff6022c385f12e7ab39367e9a73679a38a150160fccc14610bd865e06`
- public release bundle：`device_verified`

当前 HIL 使用 `c1dc5bb` Firmware 与 `3f207a5` Server。Server 的 bounded WSS catch-up 只处理尚未 decode 的孤立
stale packet；catch-up 不收敛、partial runner push、UDP stale 和真实 backpressure 仍 fail closed。Firmware 由 clean
source 和 ignored/private Kconfig input 构建；private input、凭据和 image 不进入 Git 或公开 release。另已从 clean
`6fddf82` source 使用 public 空凭据配置 fresh 构建 bundle，并在 ESP32-S3 revision 0.2 上完成 boot、临时 NVS
provisioning、WSS和UDP真机门禁。若正式 tag不再指向该 source identity，仍须从最终 tag source重建；docs-only
successor可以继承代码行为证据，但最终 artifact identity和构建校验必须重新生成。

## 当前候选身份

- 分支：`codex/provisioning-weaknet-v0.2`
- 当前实现 source：`02c8154`（包含 provisioning、capacity/churn、Desktop lifecycle 与 netem harness）
- 状态：实现已形成可恢复本地提交；最终push、fresh bundle provenance与真机HIL完成前仍不是release-ready
- 当前新鲜host门禁：root `74 passed`、Server `367 passed, 3 skipped`、Desktop Reference `124 passed`
- provisioning tools：`34 passed`；官方ESP-IDF 5.5.2 generator生成`24576-byte` NVS image通过
- 当前快照的fresh public bundle build、flash、provision、readback、NVS preserve/erase和双协议HIL：`not_run`

本节的host/远端结果绑定上述实现提交；artifact evidence仍必须由clean source构建生成的provenance和digest补齐。

## 软件与构建门禁

本轮候选新增公共 bundle provisioning CLI、容量/churn 与 netns/netem harness；尚不存在新的fresh bundle digest，
因此不替换上节已登记 artifact identity。当前 host 验证事实为：provisioning tools
`34 passed`，并使用官方 ESP-IDF 5.5.2 NVS generator成功生成固定 `24576-byte` image；capacity harness已对 WSS与
UDP执行 1/5并发阶梯并标记 `measured`，每个成功 session要求非零上行、固定下行帧数、唯一 playback闭环和必需
事件，同时验证 Worker `active_sessions=0`、子进程/端口回收及 orphan task修复。第一条测试lease的释放由随后
epoch/fencing前进的reacquire证明；最终测试lease只验证release request被接受。1/5均为单Worker场景，不提供
multi-Worker drain证据。上述是当前实现source的host evidence，不属于fresh firmware artifact或真机证据。

早期8-cell、1-repeat Linux snapshot只是一项已被扩展矩阵取代的小样本。首次完整
`9 scenarios x 2 profiles x 5 repeats`运行共90次，WSS全部满足，但UDP在3% random loss有1/5、5% random loss有
2/5失败；失败均实际丢失1个UDP包，session/route清理正常。当前修复把结果严格区分为完整`completed`和
`bounded_recovery_verified`：后者只允许用于实际发生UDP loss且明确观察到playback stopped、fresh session identity、
旧媒体未恢复、active session归零与route reacquire的场景；non-loss场景不得用恢复替代成功。修复后的完整矩阵
fresh rerun为`all_expectations_met=true`、`attempts=90`：83次完整`completed`、2次严格
`bounded_recovery_verified`、5次预期`udp_probe_timeout`，每个scenario/profile组均为5/5。目标`tc`不支持seed，
aggregate为`evidence_scope=completion_and_bounded_recovery`、`paired_randomness=false`、
`comparison_limit=completion_and_bounded_recovery_unpaired_random_impairment`；没有media age、late/loss/PLC或物理播放
数据时仍不得声明性能SLO或transport优劣。

远端UDP short-session churn已连续运行`1800.038s`并完成`17855/17855`个严格媒体闭环，p50/p95/p99为
`32.523/42.102/58.014ms`、最大`181.659ms`。远端WSS也连续运行`1800.050s`并完成`18013/18013`，
p50/p95/p99为`31.849/41.710/49.143ms`、最大`144.454ms`。两轮最终active session均为0且进程/端口回收通过。
2小时short-session churn与continuous-session soak均为`not_run`，后者runner尚未实现。使用新CLI进行真机
flash/provision/readback/preserve/erase也为`not_run`。单个profile通过不能外推为另一profile或双profile门禁通过。

历史 GitHub Actions 已覆盖 `repository`、`server`、`desktop-reference`、Linux host E2E、Redis integration、native
host contracts 和 ESP-IDF build/size。当前 `3f207a5` 本地完整门禁为 root `52 passed`、Server
`310 passed, 3 skipped`、Desktop Reference `116 passed, 4 deselected`；Ruff、repository contracts、tracked/untracked
secret scan 与 `git diff --check` 均通过。3 个 Server skip 是未配置 Redis subprocess URL，4 个 Desktop deselect 是
Linux-only deterministic host E2E。Server source `3f207a5` 的 GitHub Actions
[run 30875773937](https://github.com/wuhuaha/realtime-voice-agent/actions/runs/30875773937) 已成功；后续
`6fddf82` 只包含 evidence 文档，不改变 Server/Firmware source，未登记独立 CI。Private deployment app 从 clean
source/config构建，大小 `0x21aa80`，4 MiB app partition余量
`0x1e5580`（47%）；其 provenance、source revision、config digest与 artifact digest 已记录。该 private app不等于
公开 release bundle。

公共 bundle 使用锁定 ESP-IDF `v5.5.2` 和 tracked `sdkconfig.defaults` 构建，六个 Wi-Fi/bootstrap敏感字段均为空。
application大小 `0x21a650`，4 MiB app partition余量 `0x1e59b0`（47%）；五个烧录镜像、分区 offset、固定字体来源、
许可证和 SHA-256 均由 manifest/provenance绑定。provenance SHA-256为
`eb3eb74223d7e534f0c5a3dbd2ee5eff276ade990bbbeb94c20f820542ecc1b8`。CycloneDX 1.5 release SBOM 从四个 lock input
确定性生成，共 103 个组件，SHA-256为 `051b3576694b46da7dad0462777325f99e5595db629dd933429e1e8c9f3fded8`，
`--check` 与 secret scan 均通过。bundle、SBOM和 provenance保持 ignored，等待正式 release流程上传，不进入 Git。

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

### Public bundle 真机门禁

公共 bundle `e63d799c...c6f3be` 的五个分区通过 `COM11` 烧录并逐段 hash verified，未把 Wi-Fi、Director endpoint或
token写入 firmware image。首次空 NVS启动按预期进入 provisioning UI；随后从 ignored local input生成临时 NVS image，
只写入 `0x9000` NVS分区并在烧录后删除临时 CSV/image。完整重启日志确认 app version `6fddf82`、ESP-IDF `v5.5.2`、
16 MB Flash、8 MB PSRAM、Qwen字体、显示/触摸、Wi-Fi、WakeNet model、AEC/VAD和codec初始化，无 panic、Task WDT或
reboot loop。MIC会话触发由日志闭环，用户另行确认 `Hi ESP` 语音唤醒正常。

公共 bundle WSS回归：

- bootstrap `200`、selected profile=`wss-opus/1`
- 两轮真实问答完成；上行 `374` 个 packet、`1122` 个 PCM frame、下行 `94` 个 packet
- `invalid_opus_packets=0`、`overload_source=none`、drop=`0`
- `close_code=1000`、`close_reason=normal`、`session_closed reason=user_initiated`
- stop阶段前两次 release连接超时，析构有界兜底下一次成功；Director exact release `200`，无 lease残留

公共 bundle UDP回归：

- bootstrap `200`、selected profile=`udp-opus-gcm/1`
- authenticated probe单次成功，`elapsed_ms=47`，Server完成source pinning
- 两轮真实问答完成；上行 `462` 个 packet、`1386` 个 PCM frame、下行 `82` 个 packet
- `invalid_opus_packets=0`、`overload_source=none`、drop=`0`
- `close_code=1000`、`close_reason=normal`、`session_closed reason=user_initiated`
- Director exact release `200`；最终 Worker `active_sessions=0/5`，Director/Worker `NRestarts=0`

## 当前发布门禁

| Gate | 当前状态 | 当前证据与完成条件 |
| --- | --- | --- |
| Product commit + CI | `host_verified` | `3f207a5` 已 push；本地完整门禁与 GitHub Actions run `30875773937`通过；`6fddf82`仅为 evidence 文档 |
| Server immutable archive | `public_path_verified` | `3f207a5` 只读 archive strict check、部署和 rollback保留通过 |
| Linux Director/Worker readiness | `public_path_verified` | 当前 release ready，profiles、capacity、provider、coordination和 UDP socket正常 |
| Native clean build + size | `build_passed / image_sized` | `6fddf82` public bundle release-eligible，五镜像/provenance/SHA-256完整，app余量47% |
| Flash/boot/display/touch | `device_verified` | public五分区hash verified；app identity、显示、触摸、Qwen字体、AFE无异常 |
| Wi-Fi/NVS/bootstrap | `device_verified` | 空NVS按预期进入provisioning；临时private NVS input未进入bundle/Git，公网bootstrap成功 |
| WSS voice loop | `device_verified` | public bundle两轮，374/94包、1122 PCM frames、normal close/release、0 invalid/overload |
| UDP admission/bootstrap | `device_verified` | public bundle完成AEAD probe、source pinning，elapsed 47 ms、invalid 0 |
| UDP voice loop | `device_verified` | public bundle两轮双向Opus，462/82包、1386 PCM frames、normal close、0 invalid/overload |
| End-to-end latency | `not_run` | alpha known limitation；未承诺固定 p50/p95/p99 SLO |
| Weak network | `host_verified / performance incomplete` | 90-cell为83 completed、2 bounded recovery、5预期UDP blocked，各组5/5；无seed且无物理/性能指标，不构成transport优劣或SLO |
| Stability / capacity | `30m measured / incomplete` | WSS/UDP远端独立30分钟分别`18013/18013`与`17855/17855`；2小时、continuous-session和multi-Worker drain未完成 |
| Public provisioning CLI HIL | `not_run` | 34项host test和官方NVS generator通过；新CLI的真机flash/provision/readback/erase尚未执行 |
| AEC/acoustic | `out_of_scope` | 当前开源定位不以 NS/ASR/AEC 主观效果为 release gate |
| Security/repository | `host_verified / production incomplete` | 当前 secret/repository scan通过；103组件 release SBOM已生成并校验；TLS、漏洞扫描和入口限流仍由部署方完成 |

## 证据规则

- `host_verified`、`public_path_verified`、`build_passed` 和 `device_verified` 不得互相替代。
- 设备结论必须绑定完整 Product commit、firmware digest 和 private config digest；不公开 private input 或 binary。
- skipped、旧 artifact 或旧日志不计为当前通过。
- 正式 tag/release 前仍需从目标 commit fresh build，并完成 release scope 中未关闭的门禁。
