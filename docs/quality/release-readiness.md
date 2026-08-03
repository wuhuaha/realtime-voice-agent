# Release readiness

更新日期：2026-08-03
状态：WSS regression fixed in source / final artifact HIL not run

本文只记录当前 Product 候选和可复核的发布门禁。历史 artifact、临时地址、SSID、原始串口日志和旧协议结论不构成
当前 release evidence；未执行的门禁保持 `not_run` 或 `incomplete`。

## 当前候选身份

- 分支：`codex/lifecycle-convergence-hardening`
- 历史真机/公网 artifact source：`ae56facccf11b99c536e5c5bc93c3e4d41602028`
- 历史真机 app SHA-256：`75c5d4f8a16cdb679ad4c5bb902acdd8bb42395d81cc0f4d76455d784a4a2dd4`
- 当前 freshness 修复 source：本文所在 commit；提交前不可标记为 durable
- 当前修复 public/private firmware artifact：`not_built_from_commit`
- 当前修复 Server deployment：`not_deployed`
- 当前修复 WSS HIL：`not_run`

`ae56fac` 是最后一个具有完整真机和公网日志的历史 identity。它的 UDP gate 完整通过；WSS 虽多次完成 ASR、LLM、
TTS 和物理播放，承载播放的两次 session 随后均以 `1013/media_overloaded` 关闭，因此 WSS final gate 未通过。当前
修复同时修改 Server media timeline 与 Firmware WSS sender，历史 device evidence 不得外推到修复版。最终 tag 前
必须从本文所在 commit fresh 构建无凭据 public firmware，并由 ignored/private Kconfig input 生成 deployment image；
private input、凭据和 image 不进入 Git 或公开 release。

## 软件与构建门禁

历史 GitHub Actions 已覆盖 `repository`、`server`、`desktop-reference`、Linux host E2E、Redis integration、native
host contracts 和 ESP-IDF build/size。当前修复的 focused Server tests 为 `41 passed`，完整 Server tests 为
`298 passed, 3 skipped`；Server Ruff、root Ruff、root `52 passed`、Desktop Reference `116 passed, 4 deselected`、
repository verifier、native runtime host tests 与 `git diff --check` 均通过。3 个 Server skip 是未配置 Redis subprocess
URL，4 个 Desktop deselect 是 Linux-only deterministic host E2E；二者已有历史 Linux/CI 证据，但当前
commit-addressable CI 仍须在 push 后成功。一次 `ae56fac-dirty` private compile 只证明源码可编译，不能作为 final
artifact、digest 或 HIL 证据。

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

当前历史部署 release 为 `rva-20260803T-ae56fac`，Worker incarnation 为
`worker-ol-rva-20260803T-ae56fac`。Director 和 Worker 均由 Linux `systemd --user` 运行；最后观测结果：

- Director：`ready`，coordination=`redis`
- Worker：`ready`，`draining=false`，`healthy=true`，`active_sessions=0/5`
- provider network、coordination、RVA WSS 和 RVA UDP socket：ready
- advertised profiles：`wss-opus/1`、`udp-opus-gcm/1`

该部署未包含当前 freshness 修复，不得作为修复版 WSS 证据。后续 replacement 必须分配唯一 `worker_id`，不得用
重复 identity 原地 restart。

同一部署上执行确定性 bootstrap smoke：WSS-only 与 WSS+UDP 两种请求均命中上述 Worker，返回预期 profiles，
随后 exact route release 成功。该 smoke 证明公网 admission/bootstrap，不替代真机 UDP media。

同一部署另使用 Desktop Reference Client、服务器 loopback WSS 和本机离线生成的中文 PCM 执行一次 real-provider
canary：119 个上行 frame、137 个下行 playback frame、1 次完整 playback fact；FunASR final、DeepSeek LLM、
MiMo TTS、normal close 和 exact release 均完成，最终 `active_sessions=0`。`turn_latency_summary` 为
`status=complete`，TTS 与 playback facts 均归属 `turn-000001`，`endpoint_playback_finished` 为
`interrupted=false`、`playback_position_ms=8220`；未观察到 `ReadTimeout`、playback terminal timeout 或异常关闭。
该 canary 不保存转写、回复或下行音频，也不替代 ESP32 DAC、声学或 UDP HIL。

本部署使用实验室 HTTP/WS 公网入口，不构成生产 TLS 门禁。正式环境仍必须提供受信 HTTPS/WSS 域名、证书校验、
入口限流和受控 secret；不得把本次公网可达性提升为 production security 通过。

## ESP32-S3 真机回归

目标为立创实战派 ESP32-S3 revision 0.2，历史 artifact `ae56fac` 的 app SHA-256 为
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
`active_sessions=0`。观察窗口未见 panic、Task WDT 或反复重启。测试环境持续存在背景人声；NS、VAD
切分和 ASR 准确率不属于本项目本轮门禁，只要求这些输入不得破坏 transport、session、playback generation、terminal
或资源释放。绑定最终 deployment image 的短 HIL 尚待执行；声学、真实 netem 和固定延迟分位数不进入本次自动证据。

## 当前发布门禁

| Gate | 当前状态 | 当前证据与完成条件 |
| --- | --- | --- |
| Product commit + CI | `incomplete` | 当前修复待 commit/push；旧 CI 不替代当前 commit-addressable CI |
| Server immutable archive | `incomplete` | `ae56fac` 正在运行；当前修复待 archive、部署和 readiness |
| Linux Director/Worker readiness | `not_run` | 历史 `ae56fac` ready；当前修复尚未部署 |
| Native clean build + size | `incomplete` | dirty compile通过但不可发布；待从当前 commit fresh build、provenance 与 size |
| Flash/boot/display/touch | `not_run` | 历史 `ae56fac` device verified；修复版未烧录 |
| Wi-Fi/NVS/bootstrap | `not_run` | 历史 `ae56fac` device verified；修复版未运行 |
| WSS voice loop | `not_run` | `ae56fac` 播放成功后两次 overload；修复版必须完成 normal close/release且无 overload |
| UDP admission/bootstrap | `not_run` | 历史 `ae56fac` device verified；修复版未运行 |
| UDP voice loop | `not_run` | 历史 `ae56fac` 双向 Opus、完整 playback fact、0 invalid；修复版未运行 |
| End-to-end latency | `not_run` | alpha known limitation；未承诺固定 p50/p95/p99 SLO |
| Weak network | `not_run` | 历史 deterministic fault matrix通过；当前 source与真实 random/burst/jitter/netem均未测 |
| Stability | `incomplete` | 历史 1804 秒/211轮通过；当前 source仅完成自动回归与待执行的短 HIL |
| AEC/acoustic | `out_of_scope` | 当前开源定位不以 NS/ASR/AEC 主观效果为 release gate |
| Security/repository | `host_verified / production incomplete` | secret scan、历史已知凭据扫描、SBOM和许可证digest通过；TLS/限流仍由部署方提供 |

## 证据规则

- `host_verified`、`public_path_verified`、`build_passed` 和 `device_verified` 不得互相替代。
- 设备结论必须绑定完整 Product commit、firmware digest 和 private config digest；不公开 private input 或 binary。
- skipped、旧 artifact 或旧日志不计为当前通过。
- 正式 tag/release 前仍需从目标 commit fresh build，并完成 release scope 中未关闭的门禁。
