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

## 构建

要求 ESP-IDF 5.5.2（revision `30aaf64524299d3bde422ca9a2848090d1bc5d0f`）：

```powershell
idf.py set-target esp32s3
idf.py build
idf.py size
```

构建不代表显示、触摸、音频、AEC、网络或 provider 已通过。烧录前记录 source identity 和 artifact digest；
实机结论写入 [Release readiness](../../../docs/quality/release-readiness.md)。

## 回滚

在 native endpoint 完成同级 WSS/UDP/UI/audio/HIL 门禁前，`firmware/targets/lichuang-dev/` 保留为 compatibility
rollback lane。新功能和协议修改只进入 native application 与 canonical `protocol/`。
