# RVA native voice terminal

`voice_terminal` 是首个 Product native ESP-IDF endpoint composition。它组合：

- `board_lichuang_s3`、`audio_pipeline` 与 `audio_frontend_esp_sr`；
- `device_config`、Wi-Fi/NVS 和 Director bootstrap；
- `rva-control-v2`、`wss-opus-v3`/`udp-opus-gcm-v2` 与 session/playback lifecycle；
- 可选 `ui_lvgl`，核心语音组件不依赖显示。

## 配置

Wi-Fi、bootstrap token 和 endpoint 不得写入源码、`sdkconfig.defaults` 或 Git。开发配置通过 Kconfig/local
build input 注入；生成的 `sdkconfig`、`managed_components/`、`build/` 和 firmware binary 保持 ignored。
生产设备应使用独立 provisioning/credential 流程，不发布开发 token。

本地开发凭据放在 ignored 的 `sdkconfig.local`。未显式传入其他 `SDKCONFIG_DEFAULTS` 时，工程会按顺序自动加载
`sdkconfig.defaults` 和存在的 `sdkconfig.local`；因此 clean build 与增量 build 使用同一配置来源。烧录前仍需核对
生成的 `sdkconfig` 和目标 build 目录，禁止从历史 `build-*` 目录烧录未确认身份的镜像。

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

```powershell
# ESP-SR 2.3.1 的模型打包脚本按 Python 默认编码读取 sdkconfig；
# Windows 上只要 local 配置含非 ASCII 文本，就必须启用 UTF-8 模式。
$env:PYTHONUTF8 = "1"
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
- Opus encode 的 task stack 不能按调用前 watermark 估算；内部峰值必须包含在内。当前 uplink task 依据实机首帧
  encode 后约 26 KiB 的使用量配置为 36 KiB，保留约 10 KiB 余量；后续调整必须继续以 HIL high-water 证据为准。
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
