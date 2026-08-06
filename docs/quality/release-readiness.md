# Release readiness

更新日期：2026-08-06
状态：当前候选 host/弱网/30m 与 fresh public bundle 双协议 HIL 已验证 / alpha 非生产就绪

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
- 当前 Product source：`4950ad3eb37f753de5c6a13689f93312eda82713`
- fresh bundle source：`b03f706c394fddedef2364e594e8fc5680473131`
- 状态：已从 clean source 构建 public bundle，并完成真机 flash、配置 NVS 与双协议 HIL；仍受本文列出的 alpha 边界约束
- 当前新鲜host门禁：root `74 passed`、Server `369 passed, 3 skipped`、Desktop Reference `124 passed`
- 当前 GitHub Actions：[run 31068975637](https://github.com/wuhuaha/realtime-voice-agent/actions/runs/31068975637) 全部成功
- provisioning tools：`34 passed`；官方ESP-IDF 5.5.2 generator生成`24576-byte` NVS image通过
- fresh public bundle SHA-256：`8cf5382eac72704b2b06de3d0a4b0d5b7a53191a4fc9deadaf2e46e7203c80e4`
- fresh application SHA-256：`7b431f10a8a7c96053b76745d965fc61b0ca8ad670626d4de336bb1711e69f14`
- fresh bundle build/manifest/checksum/CLI validate与五镜像flash dry-run：`build_passed / host_verified`
- fresh bundle真机flash、provision/readback、NVS preserve/erase和双协议HIL：`device_verified`

当前 Product source 在 fresh bundle source 后只增加 HIL 证据和 host capacity harness修复；没有改变 Firmware或Server
runtime。host/CI结果绑定当前 Product source；bundle evidence继续绑定精确clean source与digest，不能替代真机HIL。

## 软件与构建门禁

本轮候选新增公共 bundle provisioning CLI、容量/churn 与 netns/netem harness，并已从 clean `b03f706` 构建新的
fresh bundle。项目显式启用 `CONFIG_ESP_WS_CLIENT_SEPARATE_TX_LOCK=y`；生成配置中的
`CONFIG_ESP_WS_CLIENT_TX_LOCK_TIMEOUT_MS=2000`
来自组件默认值，并非项目主动调大超时。独立 TX lock 使全双工 WSS 上行发送不再与下行接收/状态主锁竞争。当前 host 验证事实为：provisioning tools
`34 passed`，并使用官方 ESP-IDF 5.5.2 NVS generator成功生成固定 `24576-byte` image；capacity harness已对 WSS与
UDP执行 1/5并发阶梯并标记 `measured`，每个成功 session要求非零上行、固定下行帧数、唯一 playback闭环和必需
事件，同时验证 Worker `active_sessions=0`、子进程/端口回收及 orphan task修复。第一条测试lease的释放由随后
epoch/fencing前进的reacquire证明；最终测试lease只验证release request被接受。1/5均为单Worker场景，不提供
multi-Worker drain证据。Linux CI另以`concurrency=5 / worker_max_sessions=2`运行三Worker WSS/UDP场景并验证drain。
capacity模式在`session.opened`后使用最长2秒的有界overlap窗口，只有真实Worker health观察到目标峰值才标记
`measured`；取消路径统一回收session/observer task。该窗口不用于churn，不改变既有30分钟自然短会话负载语义。
上述是当前实现source的host evidence，不属于fresh firmware artifact或真机证据。

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
2小时short-session churn仍为`not_run`。UDP设备continuous-operation soak已于2026-08-06完成：设备从
14:11:59到16:30:24保持UDP聆听体验并在终点normal close，覆盖约2小时18分钟。该run不是单个加密session永久
存活；设备按约10分钟monotonic freshness deadline执行fresh bootstrap和换钥。它不替代WSS 2小时、2小时
short-session churn、24小时稳定性或真实容量测量。

GitHub Actions 已覆盖 `repository`、`server`、`desktop-reference`、Linux host E2E、Redis integration、native
host contracts 和 ESP-IDF build/size。当前 `3f207a5` 本地完整门禁为 root `52 passed`、Server
`310 passed, 3 skipped`、Desktop Reference `116 passed, 4 deselected`；Ruff、repository contracts、tracked/untracked
secret scan 与 `git diff --check` 均通过。3 个 Server skip 是未配置 Redis subprocess URL，4 个 Desktop deselect 是
Linux-only deterministic host E2E。Server source `3f207a5` 的 GitHub Actions
[run 30875773937](https://github.com/wuhuaha/realtime-voice-agent/actions/runs/30875773937) 已成功；后续
`6fddf82` 只包含 evidence 文档，不改变 Server/Firmware source。当前 Product source `4950ad3` 的
[run 31068975637](https://github.com/wuhuaha/realtime-voice-agent/actions/runs/31068975637) 已全部成功，包含 Linux
Server capacity、双协议 Desktop E2E、Redis、native host contracts和ESP-IDF build/size。Private deployment app 从 clean
source/config构建，大小 `0x21aa80`，4 MiB app partition余量
`0x1e5580`（47%）；其 provenance、source revision、config digest与 artifact digest 已记录。该 private app不等于
公开 release bundle。

当前 fresh 公共 bundle 使用锁定 ESP-IDF `v5.5.2` 和 tracked `sdkconfig.defaults` 构建，六个 Wi-Fi/bootstrap敏感字段均为空。
application大小 `0x21a9c0`，4 MiB app partition余量 `0x1e5640`（47%）；五个烧录镜像、分区 offset、固定字体来源、
许可证和 SHA-256 均由 manifest/provenance绑定。当前 build provenance SHA-256为
`3f0ded37bc28219c56ac043829cccf1ac96751f75a9f78677400ba2ae3ad463f`。依赖 lock 未改变；已从当前 Product source
重新生成 CycloneDX 1.5 SBOM并执行`--check`，共103个component，SHA-256为
`051b3576694b46da7dad0462777325f99e5595db629dd933429e1e8c9f3fded8`。SBOM是锁定依赖清单，不是漏洞扫描或
许可证复核结论。bundle、SBOM和 provenance保持 ignored，等待正式 release流程上传，不进入 Git。

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

### 上一 Public bundle 真机门禁

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

### 当前 Fresh public bundle 真机门禁

最初从 `e0ea539408cee2c785c596b76d8c626d63ccc4cf` 构建的 fresh bundle 已被 supersede：真机 WSS 全双工时，
`esp_websocket_client` 下行接收持有主锁，上行 Opus send 等待约 250 ms 后报
`Could not lock ws-client`，继而触发 `runtime failure category=uplink_send` 和 fresh reconnect。该 artifact 不能作为最终
WSS 发布证据。`b03f706c394fddedef2364e594e8fc5680473131` 通过独立 TX lock 修复该竞争，并从 clean source 重新构建；
当前 bundle SHA-256 为 `8cf5382eac72704b2b06de3d0a4b0d5b7a53191a4fc9deadaf2e46e7203c80e4`，application
SHA-256 为 `7b431f10a8a7c96053b76745d965fc61b0ca8ad670626d4de336bb1711e69f14`。

上游 `espressif/esp_websocket_client 1.7.0` 的 PING/PONG/CLOSE 内部路径仍把名称和说明为毫秒的 TX lock timeout
直接传给 FreeRTOS semaphore API，实际等待时长受 tick rate 影响。这是依赖版本的残余单位风险；当前数据面修复不以
调大该值掩盖竞争，后续升级组件时应复核并用故障注入覆盖控制帧路径。

当前 bundle 的五个镜像均完成真机 flash 并逐段 hash verified。配置工具的 provision/readback 和 erase-config 路径已
在本轮真机流程验证；修复版五镜像 reflash 保留 NVS，`rva_wifi:ssid`、`rva_wifi:password`、
`voice_agent:ws_url`、`voice_agent:token_origin`、`voice_agent:token` 五个业务键前后值一致，NVS 页 CRC 均为 `OK`。

当前 fresh bundle WSS 回归：

- selected profile=`wss-opus/1`；上行 `928` 个 packet，下行 `266` 个 packet
- 会话 normal close/release，最终 Worker `active_sessions=0`
- 未再观察到 `Could not lock ws-client`、`runtime failure category=uplink_send` 或非预期 fresh reconnect

当前 fresh bundle UDP 回归：

- selected profile=`udp-opus-gcm/1`；authenticated probe 单次成功，`elapsed_ms=12`
- 上行 `533` 个 packet、`1599` 个 decoded/runner PCM frame，下行 `135` 个 packet
- 用户确认真实交互符合预期；服务端会话 normal close/release，最终 Worker `active_sessions=0`
- 串口采集因 Windows USB 重枚举中止，未取得完整串口尾段；该缺口不写成串口通过，UDP 结论绑定用户体验与完整服务端
  media/session evidence

### UDP continuous-operation 长稳

2026-08-06使用同一块立创实战派ESP32-S3、fresh `b03f706` firmware、Linux release
`rva-20260804T034936Z-3f207a5`和`udp-opus-gcm/1`执行真实设备长稳。观察窗口为14:11:59至16:30:24，
有效时长约2小时18分钟。中点和终点固定语句均完成ASR、TTS与物理播放确认；终点目标回复
`playback_position_ms=3150`、`interrupted=false`。MIC stop后Server记录`close_code=1000`、
`close_reason=normal`、`session_closed reason=user_initiated`，最终Worker `active_sessions=0/5`。

设备串口在稳定阶段记录8次计划内monotonic grant refresh；每次均先exact route release，再fresh bootstrap、
authenticated UDP probe和AFE恢复，未发生transport fallback。稳定阶段持续报告`drops=0/0`、
`presend_stale=0`、`wss_send_fail=0`，AFE ring free约`0.99-1.00`，无panic、Task WDT或reboot。

开始后约6分钟曾出现一组有界恢复：两次`invalid_media_timestamp`、两次`opus_input_stale`和一次握手超时导致
fresh session重建；设备自动恢复，随后约132分钟仅发生计划内refresh。该finding不写成“全程零重连”，也不归因于
声学；它证明当前fail-closed/fresh-reopen逃生路径有效，但Server media timeline和冷启动瞬态仍保留为后续诊断项。
观察期间另有一次LLM `504 queue_timeout`自动重试成功，未导致session关闭。原始串口和systemd journal保持ignored或
远端受控保存，不进入Git。

### UDP cold-entry timeline candidate

后续确定性测试按真实日志复现了两个边界：`10560 samples / 56 ms arrival`和
`12480 samples / 2 ms arrival`在baseline均触发`oversized_future_jump`。Firmware `UplinkFramer`在latest-wins
丢帧时有意继续推进media timestamp，因此这些已认证、cadence合法且无Server backlog的UDP gap可以表示当前live edge，
不应直接归类为协议攻击。

Candidate修复只允许UDP在两个freshness window内、queue无backlog/pending put时reanchor，并与isolated stale共用
10秒最多2次的恢复预算；第三次、超界、uint32倒退/模糊半区、duplicate、non-cadenced和WSS burst仍fail closed。
它不扩大queue、不伪造PCM/PLC、不放宽AEAD/replay/generation admission，也不让UDP进入WSS catch-up。

验证结果：聚焦11项、Server `376 passed, 3 skipped`、Product `74 passed`、native/UDP host tests和ESP-IDF 5.5.2
build/size均通过；app分区余量47%。只读验证release
`rva-20260806T091641Z-bf98119-wt9272aa5c`完成一轮真实UDP probe、ASR、TTS和播放，无protocol error、stale或重连。
本轮未自然命中forward-gap分支，因此只证明确定性行为和真机无明显劣化，不能替代重复长稳。该HIL release由
`bf98119`加worktree patch构成，patch SHA-256为
`9272aa5cb907ab5632de7c035cb15d62f1e346bfbc90b71a5856d7e7c601a0d7`。

相同实现已形成clean Product commit `d5bace0e5246b9c92d5158cddc421c19a565078b`，并以Git archive部署为
`rva-20260806T093500Z-d5bace0`；archive SHA-256为
`ee892ffacaedcc3eb1242269951846b967a19a7e52c05b8b1ab5669bd450eaac`。clean release readiness为200，
UDP、provider与coordination均ready，但未在该clean release上重复HIL。因此真机证据仍绑定内容等价的临时release，
clean release只证明commit-addressable部署与启动就绪，不能冒充真机回归证据。

## 当前发布门禁

| Gate | 当前状态 | 当前证据与完成条件 |
| --- | --- | --- |
| Product commit + CI | `host_verified` | 当前 `4950ad3` 本地root 74项、Server 369项通过；GitHub Actions run `31068975637`全部成功 |
| Server immutable archive | `public_path_verified` | `3f207a5` 只读 archive strict check、部署和 rollback保留通过 |
| Linux Director/Worker readiness | `public_path_verified` | 当前 release ready，profiles、capacity、provider、coordination和 UDP socket正常 |
| Native clean build + size | `build_passed / image_sized` | fresh `b03f706` bundle SHA-256 `8cf5382e...c80e4`；application `0x21a9c0`，app余量47% |
| Flash/boot/display/touch | `device_verified` | 当前 fresh public五分区hash verified；app identity、显示、触摸、Qwen字体、AFE无异常 |
| Wi-Fi/NVS/bootstrap | `device_verified` | provision/readback与erase-config已验证；修复版reflash后五个业务NVS键保持一致，公网bootstrap成功 |
| WSS voice loop | `device_verified` | 当前 fresh bundle 928/266包、normal close/release、active 0，无锁失败或非预期重连 |
| UDP admission/bootstrap | `device_verified` | 当前 fresh bundle完成AEAD probe与source pinning，elapsed 12 ms |
| UDP voice loop | `device_verified / serial_partial` | 当前 fresh bundle 533/135包、1599 PCM frames、normal close、active 0；用户体验和Server evidence通过，串口因USB重枚举中止 |
| End-to-end latency | `not_run` | alpha known limitation；未承诺固定 p50/p95/p99 SLO |
| Weak network | `host_verified / performance incomplete` | 90-cell为83 completed、2 bounded recovery、5预期UDP blocked，各组5/5；无seed且无物理/性能指标，不构成transport优劣或SLO |
| Stability / capacity | `UDP 2h device measured / incomplete` | UDP continuous-operation约2小时18分钟，8次计划内freshness refresh后继续运行并normal close；启动早期有5次有界session/bootstrap failure finding。WSS 2小时、2小时short-session churn、24小时和真实容量测量未完成 |
| Public provisioning CLI HIL | `device_verified` | fresh bundle validate、五镜像flash、provision/readback、五键preserve与erase-config真机路径通过 |
| AEC/acoustic | `out_of_scope` | 当前开源定位不以 NS/ASR/AEC 主观效果为 release gate |
| Security/repository | `host_verified / production incomplete` | 当前 secret/repository scan通过；103-component SBOM已重新生成并通过确定性检查；TLS、漏洞扫描、许可证复核和入口限流仍由部署方完成 |

## 证据规则

- `host_verified`、`public_path_verified`、`build_passed` 和 `device_verified` 不得互相替代。
- 设备结论必须绑定完整 Product commit、firmware digest 和 private config digest；不公开 private input 或 binary。
- skipped、旧 artifact 或旧日志不计为当前通过。
- 正式 tag/release 前仍需从目标 commit fresh build，并完成 release scope 中未关闭的门禁。
