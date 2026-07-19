# Firmware

- `reference/xiaozhi-overlay/`：固定 Xiaozhi upstream 上的完整功能 reference 与复现工具；
- `locks/`：reference 构建所需的受控 dependency lock；
- `device/`：目标 component 架构的 headless contracts/core 起点，当前不是 release firmware。

完整功能复现和目标 component 抽取必须分阶段验证，不能以 `device` headless build 替代
reference 的 LCD、touch、audio、AEC、WSS/UDP、provisioning 或声学证据。
