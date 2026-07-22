# RVA native voice terminal

`voice_terminal` 是首个 Product native ESP-IDF endpoint composition。它组合：

- `board_lichuang_s3`、`audio_pipeline` 与 `audio_frontend_esp_sr`；
- `device_config`、Wi-Fi/NVS 和 Director bootstrap；
- `rva-control-v1`、WSS/UDP Opus transport 与 session/generation lifecycle；
- 可选 `ui_lvgl`，核心语音组件不依赖显示。

## 配置

Wi-Fi、bootstrap token 和 endpoint 不得写入源码、`sdkconfig.defaults` 或 Git。开发配置通过 Kconfig/local
build input 注入；生成的 `sdkconfig`、`managed_components/`、`build/` 和 firmware binary 保持 ignored。
生产设备应使用独立 provisioning/credential 流程，不发布开发 token。

设置页只允许编辑 Director bootstrap URL。设备仅会复用与已 provisioned credential 完全相同 origin
（scheme、host、port）的 token；界面不会显示 token，也不会把 token 拼入 URL。跨 origin 切换必须通过安全的
credential reprovision 流程重新绑定，不能仅在屏幕上修改地址。

## 构建

要求 ESP-IDF 5.5.2（revision `30aaf64524299d3bde422ca9a2848090d1bc5d0f`）：

```powershell
idf.py set-target esp32s3
idf.py build
idf.py size
```

完整中文字体使用独立 `font_assets` 分区，不占用 4 MiB application partition，也不把约 1.7 MiB
字形数据提交到 Git。首次构建或字体缓存不存在时，使用本机 Node.js 显式生成经过 SHA-256 校验的
Noto CJK Sans 2.004 + `lv_font_conv` 1.5.3 字体资产：

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

设备启动时会校验分区 header、边界和字体 SHA-256。分区未烧录、版本不兼容、内容损坏或描述符无法解析时，
日志会输出 `partition_missing`、`header_invalid`、`integrity_failed` 或 `descriptor_invalid`，界面降级到 LVGL
内置常用中文字库；降级可保证配置页可操作，但不保证任意 ASR/TTS 中文字符均可显示。

构建不代表显示、触摸、音频、AEC、网络或 provider 已通过。烧录前记录 source identity 和 artifact digest；
实机结论写入 [Release readiness](../../../docs/quality/release-readiness.md)。

## 音频与 WSS 诊断边界

- Opus encode 的 task stack 不能按调用前 watermark 估算；内部峰值必须包含在内。当前 uplink task 采用 pinned
  `esp_audio_codec` all-encoder test 的 40 KiB baseline，只有新的 HIL high-water 证据才能下调。
- `esp_websocket_client` 会自动处理 PING/PONG，并另外派发 CLOSED/DISCONNECTED。Transport callback 不得把
  PING/PONG/CLOSE 的零长度 DATA event 送入 RVA frame assembler，也不得在 callback 内 teardown。
- `esp_ae_rate_cvt_process()` 的 output capacity 必须来自 `esp_ae_rate_cvt_get_max_out_sample_num()`；不能把
  nominal output sample count 当作最大写入容量。
- `esp_opus_enc_process()` 的 input/output frame size 与 alignment 来自 frame-info API。协议 MTU buffer 可以更大，
  但传给 codec 的单帧 output length 使用其推荐 frame size。

## 回滚

在 native endpoint 完成同级 WSS/UDP/UI/audio/HIL 门禁前，`firmware/targets/lichuang-dev/` 保留为 compatibility
rollback lane。新功能和协议修改只进入 native application 与 canonical `protocol/`。
