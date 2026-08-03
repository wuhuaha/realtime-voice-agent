# Release readiness

更新日期：2026-08-03
状态：not ready

本文只记录当前 Product 候选和可复核的发布门禁。历史 artifact、临时地址、SSID、原始串口日志和旧协议结论不构成
当前 release evidence；未执行的门禁保持 `not_run` 或 `incomplete`。

## 当前候选身份

- 分支：`codex/lifecycle-convergence-hardening`
- Product Server candidate：`6d2be99ee5ae44c921141d2e9dcc2b69f55646c0`
- ESP32-S3 HIL firmware source：`38ebe57af4d1a3fa0fae01ad96a210fc07d57bbe`
- Server source archive SHA-256：
  `f396b1a8f6cf9516620e041297f6d38e6b8f20ed1ec7a79c186856e5232e4344`
- 公共无凭据 firmware bundle SHA-256：
  `4a95f91c9c36f71f912e02b5e4d9ac003d455f48abaa47870e5dbe9aba76638c`
- 公共无凭据 app image SHA-256：
  `64f1e4b95397419270b604d610e86f3b7f43d2b8e15870385a8c96250fd5e6b1`
- 真机 deployment app image SHA-256：
  `a3c021b92cb9d35cfa872be8a06845e47224b507286ccd2651433773f900a904`
- 真机 private sdkconfig digest：
  `960d1a7e41bf0604a827e0e5430195b2b9962b2a20b6aef6599a0965c5be0557`

公共 image 不包含 Wi-Fi、bootstrap token 或 endpoint。真机 deployment image 构建于 `38ebe57`，只通过 Git
ignored/private Kconfig input 注入部署配置；private input 和 image 不进入 Git 或公开 release。`38ebe57` 相对
`7c34337` 未修改 firmware source；`6d2be99` 只修改 Server 播放观测归属及其测试，未改变 firmware、wire、媒体热路径
或 provider 行为。最终 tag 前仍须从最终 Product commit fresh 构建公共 firmware；跨 source identity 只允许继承经
diff 证明未受影响的证据范围。

## 软件与构建门禁

GitHub Actions [`ci` run 30779989105](https://github.com/wuhuaha/realtime-voice-agent/actions/runs/30779989105)
在 commit `6d2be99ee5ae44c921141d2e9dcc2b69f55646c0` 上完成，以下 7 个 job 全部成功：
`repository`、`server`、`desktop-reference`、`desktop-reference-host-e2e`、`redis-integration`、
`native-firmware-host-contracts` 和 `native-firmware-build-size`。

公共和真机 deployment image 均通过 `scripts/build-firmware.ps1` 固定入口构建，使用 ESP-IDF
`5.5.2@30aaf64524299d3bde422ca9a2848090d1bc5d0f`、
Xtensa compiler `esp-14.2.0_20251107`、CMake `3.30.2` 和 Ninja `1.12.1` clean build。应用大小
`0x21a930`，4 MiB app partition 剩余 `0x1e56d0`（47%）。构建成功；仅 gdbinit 生成因非调试 shell
未设置 `ESP_ROM_ELF_DIR` 产生非致命 warning，不影响 application、bootloader 或烧录产物。

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
或资源释放。长稳、弱网、声学和固定延迟样本仍未执行。

## 当前发布门禁

| Gate | 当前状态 | 当前证据与完成条件 |
| --- | --- | --- |
| Product commit + CI | `passed` | `6d2be99` commit-addressable CI 7/7 jobs 成功 |
| Server immutable archive | `passed` | archive digest、只读 release、rollback identity 已记录 |
| Linux Director/Worker readiness | `passed` | Redis coordination、provider、WSS/UDP socket、capacity 与 heartbeat ready |
| Native clean build + size | `passed` | 锁定工具链、image digest、47% app partition 余量已记录 |
| Flash/boot/display/touch | `device_verified` | 完整分区写入校验；配置页退出、AFE 启动、MIC start/stop 生效 |
| Wi-Fi/NVS/bootstrap | `device_verified` | private-configured image 自动联网并完成公网 bootstrap；Wi-Fi flap 未执行 |
| WSS voice loop | `device_verified` | 当前真机单轮与 Server `6d2be99` Desktop real-provider canary 均通过 |
| UDP admission/bootstrap | `device_verified` | 真机 bootstrap、authenticated probe、source pinning、normal close/release 通过 |
| UDP voice loop | `device_verified` | 真机双向 Opus、完整 playback fact、0 invalid/drop/deadline miss 通过 |
| End-to-end latency | `not_run` | 需按固定样本量报告口径与 p50/p95/p99 |
| Weak network | `not_run` | 需覆盖 loss、burst、jitter、late、fresh reopen 与 generation fence |
| Stability | `incomplete` | 本轮多轮会话正常；尚未绑定固定 30 分钟或 2 小时 soak |
| AEC/acoustic | `not_run` | 不属于本批次；若进入 release scope 需专项执行或形成 waiver |
| Security/repository | `incomplete` | secret 未进入 Git/public artifact；SBOM、许可证复核和正式 release 记录未完成 |

## 证据规则

- `host_verified`、`public_path_verified`、`build_passed` 和 `device_verified` 不得互相替代。
- 设备结论必须绑定完整 Product commit、firmware digest 和 private config digest；不公开 private input 或 binary。
- skipped、旧 artifact 或旧日志不计为当前通过。
- 正式 tag/release 前仍需从目标 commit fresh build，并完成 release scope 中未关闭的门禁。
