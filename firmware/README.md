# Firmware

- `targets/lichuang-dev/`：唯一 production firmware composition；由 pinned Xiaozhi upstream、仓内 overlay、
  managed overlay、受控 lock 与复现脚本组成；
- `locks/`：production target 构建所需的受控 dependency lock；
- `device/`：non-release component-extraction prototype，仅用于 contracts/core 的边界验证。

目录收口只确定 production source 的唯一入口，不代表 release readiness。`device` 的 headless build 不能替代
production target 的 clean build、LCD/touch、audio、AEC、WSS/UDP、provisioning、声学或长稳证据。当前证据与
未运行项以 [MIGRATION_STATUS.md](MIGRATION_STATUS.md) 为准。
