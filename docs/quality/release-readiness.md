# Release readiness

更新日期：2026-08-02
状态：not ready

本文只记录当前 Product 候选和可复核的发布门禁。历史 artifact、临时地址、SSID、原始串口日志和旧协议结论不构成
当前 release evidence；未执行的门禁保持 `not_run` 或 `incomplete`。

## 当前候选身份

- 分支：`codex/lifecycle-convergence-hardening`
- Product commit：`926054b0120b27f1e182f9c7a5e22fdf228dc860`
- Server source archive SHA-256：
  `0dc58ef7b32adbbc8ebed99b182f318474e41c3e47114c3937cdf839f0ed5a12`
- 公共无凭据 app image SHA-256：
  `bbe1dca5738f42a412e59fe3b18dcd6af954606a3e4fdf404cf12ec3d3d70210`
- 真机 deployment app image SHA-256：
  `308885f00ace3a81f324d9e7343048c1de5daac262814ed0d1e3313b7481f025`
- 真机 private sdkconfig digest：
  `200566facaa759a65f1a1b3abe39dee3da72ed8b51bcf505568d3856840ed109`

公共 image 不包含 Wi-Fi、bootstrap token 或 endpoint。真机 deployment image 来自同一 Product commit，只通过
Git ignored/private Kconfig input 注入部署配置；private input 和 image 不进入 Git 或公开 release。二者不能互相继承
设备结论。

## 软件与构建门禁

GitHub Actions [`ci` run 30748197701](https://github.com/wuhuaha/realtime-voice-agent/actions/runs/30748197701)
在 commit `926054b0120b27f1e182f9c7a5e22fdf228dc860` 上完成，以下 7 个 job 全部成功：
`repository`、`server`、`desktop-reference`、`desktop-reference-host-e2e`、`redis-integration`、
`native-firmware-host-contracts` 和 `native-firmware-build-size`。

真机 deployment image 使用 ESP-IDF `5.5.2@30aaf64524299d3bde422ca9a2848090d1bc5d0f`、
Xtensa compiler `esp-14.2.0_20251107`、CMake `3.30.2` 和 Ninja `1.12.1` clean build。应用大小
`0x21a930`，4 MiB app partition 剩余 `0x1e56d0`（47%）。构建成功；仅 gdbinit 生成因非调试 shell
未设置 `ESP_ROM_ELF_DIR` 产生非致命 warning，不影响 application、bootloader 或烧录产物。

## Linux 公网部署

Server archive 解包为只读 release：
`/home/ubuntu/services/realtime-voice-agent/releases/rva-20260802T-926054b`，`current` 已切换到该目录；
上一 release 保留为 `current.previous-8a293f3`，未删除。

当前 Worker incarnation 为 `worker-ol-20260802T-926054b`。Director 和 Worker 均由 Linux
`systemd --user` 运行并为 `active`。最终 readiness 结果：

- Director：`ready`，coordination=`redis`
- Worker：`ready`，`draining=false`，`healthy=true`，`active_sessions=0/5`
- provider network、coordination、RVA WSS 和 RVA UDP socket：ready
- advertised profiles：`wss-opus/1`、`udp-opus-gcm/1`

首次切换后 readiness 曾因同一 Worker incarnation 在部署过程中被优雅停止而继承 one-way drain，表现为
`draining=true`。确认 `active_sessions=0` 后，停止 Worker、只清除该 incarnation 的 Redis drain key 并重新启动；
readiness 和 heartbeat 随后稳定为 `draining=false`。该过程没有关闭 readiness gate、修改 provider 或删除回滚 release。
后续部署必须为每次实际启动分配唯一 Worker incarnation，避免在新 identity 激活后再次预启动/停止。

同一部署上执行确定性 bootstrap smoke：WSS-only 与 WSS+UDP 两种请求均命中上述 Worker，返回预期 profiles，
随后 exact route release 成功。该 smoke 证明公网 admission/bootstrap，不替代真机 UDP media。

## ESP32-S3 真机回归

目标为立创实战派 ESP32-S3 revision 0.2，串口 `COM11`。先完整写入同 commit 的 bootloader、partition table、
公共 app、ESP-SR model 与字体分区，所有区域均由 esptool 报告 `Hash of data verified`；随后只补烧 private-configured
deployment app，校验通过。全过程未执行整片擦除，NVS partition 未被烧录命令覆盖。

deployment image 启动后完成已配置 Wi-Fi 与 Director endpoint 解析；AFE 初始化并持续输出健康窗口，说明启动已越过
阻塞式 provisioning 门禁。设备通过触屏 MIC 建立真实 WSS 会话，服务端记录：

- bootstrap `200`、connect grant consume `200`
- `rva_session_opened`，selected profile=`wss-opus/1`
- 3 个真实 turn 完成上行、FunASR final、LLM、MiMo TTS 和板端 playback fact
- 关闭前上行 `1374` 个 WSS packet、`4122` 个 decoded PCM frame，`invalid_opus_packets=0`
- 下行 `206` 个 packet；已记录自然完成的 `endpoint_playback_finished`
- MIC stop 后 `close_code=1000`、`close_reason=normal`、`session_closed reason=user_initiated`
- Director exact release `200`；最终 Worker `active_sessions=0`

观察窗口未见 panic、Task WDT、反复重启、非预期重连、media overload 或旧 generation 恢复。本轮只验证 WSS
真实媒体；UDP 真机媒体、长稳、弱网、声学和固定延迟采样未执行。

## 当前发布门禁

| Gate | 当前状态 | 当前证据与完成条件 |
| --- | --- | --- |
| Product commit + CI | `passed` | commit-addressable CI 7/7 jobs 成功 |
| Server immutable archive | `passed` | archive digest、只读 release、rollback identity 已记录 |
| Linux Director/Worker readiness | `passed` | Redis coordination、provider、WSS/UDP socket、capacity 与 heartbeat ready |
| Native clean build + size | `passed` | 锁定工具链、image digest、47% app partition 余量已记录 |
| Flash/boot/display/touch | `device_verified` | 完整分区写入校验；配置页退出、AFE 启动、MIC start/stop 生效 |
| Wi-Fi/NVS/bootstrap | `device_verified` | private-configured image 自动联网并完成公网 bootstrap；Wi-Fi flap 未执行 |
| WSS voice loop | `device_verified` | 3 个真实 turn、完整 playback fact、normal close 与 exact release |
| UDP admission/bootstrap | `public_path_verified` | host bootstrap/profile/release 通过；真机 UDP media 仍为 `not_run` |
| UDP voice loop | `not_run` | 当前 deployment image 仍需 authenticated probe、双向媒体和 normal close |
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
