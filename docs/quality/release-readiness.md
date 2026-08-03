# Release readiness

更新日期：2026-08-03
状态：awaiting final HIL

本文只记录当前 Product 候选和可复核的发布门禁。历史 artifact、临时地址、SSID、原始串口日志和旧协议结论不构成
当前 release evidence；未执行的门禁保持 `not_run` 或 `incomplete`。

## 当前候选身份

- 分支：`codex/lifecycle-convergence-hardening`
- Product release candidate：`d8400363ee7f0e8c3f7af88547c3b20a6da70f58`
- Product Server candidate：`6d2be99ee5ae44c921141d2e9dcc2b69f55646c0`
- ESP32-S3 HIL firmware source：`38ebe57af4d1a3fa0fae01ad96a210fc07d57bbe`
- 待部署 Product source archive SHA-256：
  `e6a397ebb0a353f1865b265c64196fcc6988ac8a26973b8c2a518d81df63697f`
- 公共无凭据 firmware bundle SHA-256：
  `5e4d1411b6b5bfca81b153bcf574b1547763309ac01dce1f2425915999581a7f`
- 公共无凭据 app image SHA-256：
  `11b940e50a75244ee9c87b53ace5b3da0c826ef057baf771115424e457974f26`
- CycloneDX 1.5 release SBOM SHA-256：
  `051b3576694b46da7dad0462777325f99e5595db629dd933429e1e8c9f3fded8`
- 真机 deployment app image SHA-256：
  `a3c021b92cb9d35cfa872be8a06845e47224b507286ccd2651433773f900a904`
- 真机 private sdkconfig digest：
  `960d1a7e41bf0604a827e0e5430195b2b9962b2a20b6aef6599a0965c5be0557`

公共 `d840036` image 不包含 Wi-Fi、bootstrap token 或 endpoint；build provenance 为 `release_eligible=true`，
绑定 clean HEAD、生成配置、`partitions.csv`、flasher manifest、固定字体包和五个分区镜像。相同输入重复打包得到
相同 bundle SHA-256，bundle 同时包含许可证、第三方声明、manifest 和 `SHA256SUMS`。真机 deployment image
构建于 `38ebe57`，只通过 Git
ignored/private Kconfig input 注入部署配置；private input 和 image 不进入 Git 或公开 release。`38ebe57` 相对
`7c34337` 未修改 firmware source；`6d2be99` 只修改 Server 播放观测归属及其测试，未改变 firmware、wire、媒体热路径
或 provider 行为。最终 tag 前仍须从最终 Product commit fresh 构建公共 firmware；跨 source identity 只允许继承经
diff 证明未受影响的证据范围。`d840036` 相对 `6d2be99` 未修改 Server production runtime；相对 `38ebe57`
未修改 firmware C/C++、wire 或媒体热路径，只增加测试、发布工具和文档。最终 HIL 仍须绑定随后烧录的 deployment
image，正式 tag/release 仍须单独授权。

## 软件与构建门禁

GitHub Actions [`ci` run 30779989105](https://github.com/wuhuaha/realtime-voice-agent/actions/runs/30779989105)
在 commit `6d2be99ee5ae44c921141d2e9dcc2b69f55646c0` 上完成，以下 7 个 job 全部成功：
`repository`、`server`、`desktop-reference`、`desktop-reference-host-e2e`、`redis-integration`、
`native-firmware-host-contracts` 和 `native-firmware-build-size`。

后续纯文档候选 `fe39a6c` 的 GitHub Actions run `30780293416` 同样为 7/7 成功。`d840036` 本地 fresh gate 结果：
root `52 passed`，Server `294 passed, 3 skipped`，Desktop Reference `116 passed, 4 deselected`；3 个 Server skip
只因本机未配置 Redis subprocess URL，Redis 与 Linux host E2E 已由 CI/独立 Linux 环境覆盖。最终推送后的
commit-addressable CI 仍须成功，不能由本地结果替代。

公共和真机 deployment image 均通过 `scripts/build-firmware.ps1` 固定入口构建，使用 ESP-IDF
`5.5.2@30aaf64524299d3bde422ca9a2848090d1bc5d0f`、
Xtensa compiler `esp-14.2.0_20251107`、CMake `3.30.2` 和 Ninja `1.12.1` clean build。应用大小
`0x21a510`，4 MiB app partition 剩余 `0x1e5af0`（47%）。公开 build、固定字体资产生成、provenance、partition
校验和确定性 package 均成功。

## 自动稳定性与故障注入

Linux 隔离 checkout 在 `fe39a6c` 上连续运行 1804 秒 deterministic churn，共 211 轮；每轮重新建立和回收独立
Director/Worker，并执行 WSS low-level、WSS DesktopApp、UDP low-level 和 UDP DesktopApp 四个 case。WSS
`422/422`、UDP `422/422` 通过，失败为 0，最长单轮 8951 ms，最终无残留进程。`d840036` 未修改被测 production
runtime，因此该证据按 diff scope 继承。

另执行 deterministic fault matrix：Server `50/50`、Desktop `63/63` 通过，覆盖 UDP authentication、replay、
gap/deadline、refresh、PLC、fresh reopen、generation fence，以及 WSS teardown、playback terminal、cancel 和 cleanup。
这些结果是 `host_verified` 协议/生命周期证据，不是物理网卡上的 random loss、burst、连续 jitter、带宽限制或
公网 TLS 测量；后者继续保持 `not_run`，UDP 继续为显式 opt-in。

## Linux 公网部署

Server archive 解包为只读 release：
`/home/ubuntu/services/realtime-voice-agent/releases/rva-20260803T-6d2be99`，`current` 已切换到该目录；
上一 release `rva-20260803T-9e5dc3a` 保留为 `current.previous-9e5dc3a`，未删除。

当前 Worker incarnation 为 `worker-ol-rva-20260803T-6d2be99`。Director 和 Worker 均由 Linux
`systemd --user` 运行并为 `active`。最终 readiness 结果：

- Director：`ready`，coordination=`redis`
- Worker：`ready`，`draining=false`，`healthy=true`，`active_sessions=0/5`
- provider network、coordination、RVA WSS 和 RVA UDP socket：ready
- advertised profiles：`wss-opus/1`、`udp-opus-gcm/1`

本次启动使用新的 Worker incarnation，未继承上一 release 的 one-way drain；部署完成后 heartbeat 持续报告
`draining=false`。后续 replacement 仍必须分配唯一 `worker_id`，不得用重复 identity 原地 restart。

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

目标为立创实战派 ESP32-S3 revision 0.2，串口 `COM11`。先完整写入同 commit 的 bootloader、partition table、
公共 app、ESP-SR model 与字体分区，所有区域均由 esptool 报告 `Hash of data verified`；随后只补烧 private-configured
deployment app，校验通过。全过程未执行整片擦除，NVS partition 未被烧录命令覆盖。

deployment image 启动后完成已配置 Wi-Fi 与 Director endpoint 解析；显示、触摸、Qwen 字体、WakeNet、
AEC `VOIP_LOW_COST`、WebRTC VAD 和双通道 AFE 均启动。当前实验 endpoint 为 HTTP，因此 SNTP 失败不会阻断
bootstrap；UDP 本地轮换使用 authenticated `refresh_after_ms` 的 monotonic deadline，Server 继续执行绝对 expiry。
设备通过 `Hi ESP` 分别建立真实 WSS 和 UDP 会话。当前候选上的 WSS 单轮完成 ASR、LLM、37 字 TTS、
`10620 ms` 完整播放和 `interrupted=false`；播放期间嘈杂环境产生的额外 VAD/ASR segment 未触发新回复或改变
playback generation。UDP 回归记录：

- bootstrap `200`、selected profile=`udp-opus-gcm/1`
- UDP socket 建立后 authenticated probe 单次成功，`elapsed_ms=47`，Server 完成 source pinning
- 用户确认真实问答完整流畅；Server 收到板端 `playback.ended`，`playback_position_ms=3690`、`interrupted=false`
- 关闭前上行 `1341` 个 UDP packet、`4023` 个 decoded PCM frame，`invalid_opus_packets=0`
- 下行 `341` 个 packet；无 media overload、旧 generation 恢复或 playback terminal timeout
- 端侧观测窗口 queue depth 为 `1/1`、drop=`0/0`，capture/frame/encode/send deadline miss 均为 `0`
- MIC stop 后 `close_code=1000`、`close_reason=normal`、`session_closed reason=user_initiated`
- Director exact release `200`；最终 Worker `active_sessions=0`

观察窗口未见 panic、Task WDT、反复重启、非预期重连或队列持续增长。测试环境持续存在背景人声；NS、VAD
切分和 ASR 准确率不属于本项目本轮门禁，只要求这些输入不得破坏 transport、session、playback generation、terminal
或资源释放。绑定最终 deployment image 的短 HIL 尚待执行；声学、真实 netem 和固定延迟分位数不进入本次自动证据。

## 当前发布门禁

| Gate | 当前状态 | 当前证据与完成条件 |
| --- | --- | --- |
| Product commit + CI | `incomplete` | `fe39a6c` CI 7/7；`d840036` 本地全绿，仍须最终 push CI |
| Server immutable archive | `prepared` | `d840036` archive digest 已记录，仍须切换只读 release 并验证 rollback identity |
| Linux Director/Worker readiness | `passed` | Redis coordination、provider、WSS/UDP socket、capacity 与 heartbeat ready |
| Native clean build + size | `passed` | `d840036` clean public build、provenance、image/bundle digest、47% app 余量通过 |
| Flash/boot/display/touch | `device_verified` | 完整分区写入校验；配置页退出、AFE 启动、MIC start/stop 生效 |
| Wi-Fi/NVS/bootstrap | `device_verified` | private-configured image 自动联网并完成公网 bootstrap；Wi-Fi flap 未执行 |
| WSS voice loop | `device_verified` | 当前真机单轮与 Server `6d2be99` Desktop real-provider canary 均通过 |
| UDP admission/bootstrap | `device_verified` | 真机 bootstrap、authenticated probe、source pinning、normal close/release 通过 |
| UDP voice loop | `device_verified` | 真机双向 Opus、完整 playback fact、0 invalid/drop/deadline miss 通过 |
| End-to-end latency | `not_run` | alpha known limitation；未承诺固定 p50/p95/p99 SLO |
| Weak network | `host_verified / measured_not_run` | deterministic fault matrix通过；真实 random/burst/jitter/netem 未测，UDP保持opt-in |
| Stability | `host_verified` | 1804 秒、211 轮、WSS/UDP 各 422 cases、0 failure、0 residual process |
| AEC/acoustic | `out_of_scope` | 当前开源定位不以 NS/ASR/AEC 主观效果为 release gate |
| Security/repository | `host_verified / production incomplete` | secret scan、历史已知凭据扫描、SBOM和许可证digest通过；TLS/限流仍由部署方提供 |

## 证据规则

- `host_verified`、`public_path_verified`、`build_passed` 和 `device_verified` 不得互相替代。
- 设备结论必须绑定完整 Product commit、firmware digest 和 private config digest；不公开 private input 或 binary。
- skipped、旧 artifact 或旧日志不计为当前通过。
- 正式 tag/release 前仍需从目标 commit fresh build，并完成 release scope 中未关闭的门禁。
