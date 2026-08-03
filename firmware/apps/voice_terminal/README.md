# RVA native voice terminal

`voice_terminal` 是首个 Product native ESP-IDF endpoint composition。它组合：

- `board_lichuang_s3`、`audio_pipeline` 与 `audio_frontend_esp_sr`；
- `device_config`、Wi-Fi/NVS 和 Director bootstrap；
- `rva/1`、`wss-opus/1`/`udp-opus-gcm/1` 与 session/playback lifecycle；
- 可选 `ui_lvgl`，核心语音组件不依赖显示。

## 配置

Wi-Fi、bootstrap token 和 endpoint 不得写入源码、`sdkconfig.defaults` 或 Git。开发配置通过 Kconfig/local
build input 注入；生成的 `sdkconfig`、`managed_components/`、`build/` 和 firmware binary 保持 ignored。
生产设备应使用独立 provisioning/credential 流程，不发布开发 token。

本地开发凭据放在 ignored 的 `sdkconfig.local`。未显式传入其他 `SDKCONFIG_DEFAULTS` 时，工程会按顺序自动加载
`sdkconfig.defaults` 和存在的 `sdkconfig.local`。这些 defaults 只在生成配置时提供初值；已有 `sdkconfig` 会继续作为
增量构建的权威输入，不会因后来修改 local defaults 而可靠更新。切换 profile、Wi-Fi 或 endpoint 时必须使用新的
`SDKCONFIG` 与 build目录，或显式重新生成配置。烧录前应检查 `config/sdkconfig.h` 中相关选项的 presence/choice，
禁止从历史 `build-*` 目录烧录未确认身份的镜像。

编译期 `RVA_DEFAULT_MEDIA_PROFILE` choice 只决定每次启动时的首选媒体 profile：Product 默认
`RVA_DEFAULT_MEDIA_PROFILE_WSS`，受控环境可在 ignored local build input 中选择
`RVA_DEFAULT_MEDIA_PROFILE_UDP`。未知、缺失或互相冲突的生成配置一律 fail-safe 到 WSS。该选项不改变设备
声明的 profile 能力、不改变 server 的最终选择，也不禁用待机 UI 上的 WSS/UDP 切换；UI 选择仍只影响下一次
session。

设置页只允许编辑 Director bootstrap URL。设备仅会复用与已 provisioned credential 完全相同 origin
（scheme、host、port）的 token；界面不会显示 token，也不会把 token 拼入 URL。跨 origin 切换必须通过安全的
credential reprovision 流程重新绑定，不能仅在屏幕上修改地址。

持续 HIL/联调应通过 provisioning 固定一个稳定的公网 bootstrap origin，使设备切换 Wi-Fi 后无需重新构建固件。
真实 host inventory、Wi-Fi credential、bootstrap token 和 provider secret 必须保存在 ignored/private 配置中，不进入
本 README、tracked defaults 或任何 firmware artifact metadata。

当前 composition 默认使用用户触发的会话生命周期：待机时由独立 `idle_wake_runtime` 独占 capture/AFE 并运行
WakeNet `Hi ESP`，唤醒命中或 MIC 点击后先有界停止 idle owner，再把音频资源交给 `VoiceRuntime`。会话期间端侧
WakeNet 不运行，打断裁决仍由服务端负责；MIC 再次点击发送显式 stop/cancel。模型不可用时降级为 MIC 启动，不能
让两个 runtime 同时持有 codec/AFE。

## 构建

要求 ESP-IDF 5.5.2（revision `30aaf64524299d3bde422ca9a2848090d1bc5d0f`）：

Windows 推荐从仓库根目录使用确定性入口；它会校验锁定的 IDF checkout，固定已安装的 Python/CMake/Ninja/
Xtensa 工具，并自动设置 ESP-SR 模型打包所需的 UTF-8 模式：

```powershell
pwsh -File .\scripts\build-firmware.ps1 -Clean
```

公开 release bundle 必须在 clean Product worktree 中使用 `-ReleaseArtifacts` 构建。在线下载不可用时可显式传入
已固定且会再次校验 size/SHA-256 的组件包；路径只作为本机构建输入，不写入 provenance：

```powershell
pwsh -File .\scripts\build-firmware.ps1 `
  -Clean -ReleaseArtifacts `
  -BuildDir firmware/apps/voice_terminal/build-release `
  -FontPackage external/78__xiaozhi-fonts-v1.6.0.zip

pwsh -File .\firmware\tools\package-release.ps1 `
  -BuildDir firmware/apps/voice_terminal/build-release `
  -Output artifacts/rva-firmware-public.zip
```

构建会写入 ignored 的 `build-provenance.json`，绑定 HEAD、构建前后仓库状态、生成配置、分区表和五个烧录镜像。
打包器只接受 `release_eligible=true` 且仍与 clean HEAD 完全匹配的产物，并把许可证、第三方声明、manifest 与
`SHA256SUMS` 一并放入 bundle；普通 dirty-tree 开发构建不会被误标为公开产物。

默认生成公共构建，配置和缓存位于 ignored 的
`firmware/apps/voice_terminal/build-local`。如需使用本地 ignored 的部署配置，必须显式传入同一工程下的
`sdkconfig` 文件和独立 build 目录：

```powershell
pwsh -File .\scripts\build-firmware.ps1 `
  -BuildDir firmware/apps/voice_terminal/build-provisioned-local `
  -Sdkconfig firmware/apps/voice_terminal/sdkconfig.provisioned-local
```

不要先运行系统安装的 `idf.py`，也不要依赖 `export.ps1`：后者会检查所有芯片工具，缺少与 ESP32-S3 无关的
`riscv32-esp-elf` 时会整体失败。脚本通过 `RVA_IDF_PATH` 和 `RVA_IDF_TOOLS_PATH` 支持在其他机器上指定
同版本的外部安装；缺失工具会在构建开始前明确报错。

```powershell
# Linux/WSL 或已手动准备好同一工具链时，也可直接运行 idf.py；Windows
# 的可复现构建应优先使用上面的脚本。
idf.py set-target esp32s3
idf.py build
idf.py size
```

完整中文字体使用独立 `font_assets` 分区，不占用 4 MiB application partition，也不把约 3.0 MiB
字形数据提交到 Git。首次构建或字体缓存不存在时，从固定的 `78/xiaozhi-fonts` 1.6.0 组件包提取
`font_noto_qwen_20_4.bin`，并校验目标 CBIN 的 SHA-256：

```powershell
idf.py font-assets
```

`idf.py flash` 会同时生成并烧录 application、partition table 和字体资产。只需补烧字体分区时使用：

```powershell
idf.py -p COMx font-assets-flash
```

`idf.py app-flash` 只更新 application，不更新字体。字体来源、许可证和固定制品身份见
[`ui_font_assets/THIRD_PARTY_NOTICES.md`](../../components/ui_font_assets/THIRD_PARTY_NOTICES.md)。构建缓存、
下载归档、`font_assets.bin` 和 firmware binary 均位于 ignored build tree，不得提交。

设备启动时会校验分区 header、边界、字体 SHA-256，以及 ASCII/中文代表字形。Qwen CBIN 使用 LVGL large
glyph descriptor，因此必须启用 `CONFIG_LV_FONT_FMT_TXT_LARGE`。分区未烧录、版本不兼容、内容损坏或字形
自检失败时，日志会输出 `partition_missing`、`header_invalid`、`integrity_failed`、`descriptor_invalid` 或
`glyph_self_test_failed`，界面降级到 LVGL
内置常用中文字库；降级可保证配置页可操作，但不保证任意 ASR/TTS 中文字符均可显示。

构建不代表显示、触摸、音频、AEC、网络或 provider 已通过。烧录前记录 source identity 和 artifact digest；
实机结论写入 [Release readiness](../../../docs/quality/release-readiness.md)。

## 音频与 WSS 诊断边界

- `model` 分区包含 WakeNet/NSNet 资产。分区缺失会关闭 idle WakeNet 和 NSNet 神经降噪；会话期 composition 仍启用
  AEC/VAD。日志行为不是声学通过证据，发布前必须验证近讲、播放中 double-talk、上行 PCM 与 ASR。
- 上行实时链路拆为 capture/AFE owner、60 ms framer、Opus encoder 和 transport sender；相邻阶段只通过容量为 2
  的固定队列交接。队列满时淘汰最旧实时帧并保留 timestamp gap，不堆积过期语音；各阶段分别记录 queue
  high-water、drop、最大 media age、执行耗时和 deadline miss。
- Opus encoder task stack 不能按调用前 watermark 估算；内部峰值必须包含在内。36 KiB 配置沿用旧固件首帧 encode
  后约 26 KiB 使用量的实机证据，但拆分后的 capture、framer、encoder、sender 和 playback 均须重新采集 HIL
  high-water 后才能认定余量充分。Host test 和成功构建不能替代该结论。
- CPU0/CPU1 idle task 的 Task WDT 均保持启用，禁止用关闭 watchdog 或单纯喂狗掩盖 deadline miss。uplink affinity
  的 unpinned、Opus-on-CPU1、audio-on-CPU1 仅用于 `EXP-RVA-001` 实验；HIL 未完成前不宣布或固化 winner。
- AEC 后音频按连续 60 ms cadence 进入启用 DTX 的 Opus encoder，本地 VAD 只保留观测用途，不门控上行、清理
  播放队列或发起打断。WSS/UDP 上行 generation 恒为 `0`。
- `esp_websocket_client` 会自动处理 PING/PONG，并另外派发 CLOSED/DISCONNECTED。Transport callback 不得把
  PING/PONG/CLOSE 的零长度 DATA event 送入 RVA frame assembler，也不得在 callback 内 teardown。
- WebSocket inner close deadline 为 1000 ms，runtime 外层 teardown watchdog 为 1500 ms。Close/destroy 无法确认时，
  设备先 best-effort release 当前 exact lease，再受控重启；不得带着未知 task/heap 状态原进程重连。
- Bootstrap response 先保留 worker/epoch/fencing release identity，再验证 endpoint/profile。Release 每轮最多尝试 2
  次，每次 HTTP timeout 800 ms、间隔 75 ms；失败保持 identity，生命周期析构前还会再执行一轮，但不会阻塞无限
  重试。每次 `Stop -> Start` 都先 release 并 fresh bootstrap，不复用旧 grant。
- UDP frame 经过 reorder/generation 后仍须通过 decode/playout 前 360 ms 最终 media-age gate；超限数据清零丢弃并
  结束当前 UDP session。
- server `playback.stop` 通过有界命令队列进入 playback task；只有该 task 执行 generation fence、清理待播队列、
  decode 和 DAC 写入。`response.end(completed)` 由 `final_media_sequence` 收口，不使用固定 drain timeout；
  `playback.started/ended` 只报告成功写入 DAC 后的物理事实。
- `esp_ae_rate_cvt_process()` 的 output capacity 必须来自 `esp_ae_rate_cvt_get_max_out_sample_num()`；不能把
  nominal output sample count 当作最大写入容量。
- `esp_opus_enc_process()` 的 input/output frame size 与 alignment 来自 frame-info API。协议 MTU buffer 可以更大，
  但传给 codec 的单帧 output length 使用其推荐 frame size。
