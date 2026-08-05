# Firmware

## 正式结构

- `apps/voice_terminal/`：Product 自有 ESP-IDF application composition。
- `components/`：board、audio/AFE、RVA protocol、WSS/UDP transport、configuration、runtime 和可选 LVGL UI。
- `device/`：独立 headless contract harness，仅消费 `components/voice_contracts` 与 `components/voice_core`，
  不提供 production runtime source，也不生成发布镜像。

核心语音与 transport 不依赖 LVGL。板级引脚、codec、I2S/TDM、显示和触摸事实集中在
`board_lichuang_s3`；应用只组合组件并拥有顶层生命周期。

当前构建、真机和未运行门禁见 [Release readiness](../docs/quality/release-readiness.md)。

开发、CI 和发布候选只从 `apps/voice_terminal/` 构建。

公开 ESP32-S3 bundle 不携带 Wi-Fi、Director URL 或 bootstrap credential。bundle 内的
`rva-device-provision.py` 提供 `validate`、五镜像 `flash`、仅 NVS 的 `provision` 和 `erase-config`；完整命令、
固定分区布局、readback 校验和凭据处理边界见 [Flashing and provisioning](release/FLASHING.md)。当前 reference
firmware 未启用 NVS encryption，物理读取 flash 的攻击者仍可能恢复 Wi-Fi 与 bootstrap credential；部署时必须使用
每设备、最小权限、可撤销的 token，不能把临时文件清理表述为静态加密或安全擦除。

当前CLI host contracts为`34 passed`；每个fresh release bundle仍须单独完成flash、provision、readback、NVS
preserve/erase和WSS/UDP HIL，不能从上一bundle继承artifact验证。
