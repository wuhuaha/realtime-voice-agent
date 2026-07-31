# 决策 0003：收口 production firmware composition

日期：2026-07-20
状态：superseded by [0005](0005-native-esp-idf-endpoint-and-rva-protocol.md)

本决策保留为当时 firmware composition 收口的历史记录。当前 production endpoint 与发布状态以决策 0005 和
[Release readiness](../quality/release-readiness.md) 为准；原 `firmware/targets/lichuang-dev/` 已退役，不再提供
当前 rollback route。

## 背景

交付仓此前将完整、可复现的 Xiaozhi upstream + overlay composition 放在
`firmware/reference/xiaozhi-overlay/`，同时在 `firmware/device/` 抽取纯 C++ component contracts。目录名让
“完整 production composition”和“迁移参考基线”混在一起，也容易把尚无板级实现的抽取目录误认为发布目标。

## 决定

- 将完整目录整体提升为 `firmware/targets/lichuang-dev/`，并作为唯一 production firmware composition。
- Production composition 保持 pinned upstream + repository-owned overlay + managed overlay + controlled lock；
  external checkout 继续 ignored，不改变固件产品内容。
- 当时把 `firmware/device/` 定义为非发布抽取工程；决策 0005 完成组件提升后，该目录仅保留独立 headless
  contract harness，production source 统一位于 `firmware/components/`。
- 当时的 migration baseline 以 `source_path` 记录原位置，以 `production_path` 记录提升后的文件；该 baseline
  已随 legacy composition 退役，当前运行、构建、测试与 CI 不使用这项关联。
- CI job 命名为 `production-source-contract`。它验证 pinned inputs、PowerShell parse、host contracts 与 ignored
  configuration handling，不声明 clean build。

## 后果

Production source ownership 与本地/CI 入口变为唯一且可检查；当时的迁移阶段需要用双字段关联 baseline 与
production path。legacy baseline 退役后，当前 verifier 不再维护这项不变量。

源码迁移完成不等于 production ready。目录提升本身不产生新的 firmware artifact，也不自动提升 clean build、
flash、boot、UI、声学、弱网或长稳的证据等级；随后实际执行的 build/flash/boot 状态以
[Release readiness](../quality/release-readiness.md) 为准。

## 替代方案

- 保留 `reference/` 并让脚本约定其为 production：不选择，目录和发布语义继续冲突。
- 立即以 `firmware/device/` 组装 release image：不选择，board/audio/transport/presentation 尚未完成等价迁移。
- 提交 materialized upstream：不选择，会扩大仓库与供应链边界，并削弱 pinned materialization 门禁。

## 复查条件

- `firmware/components` 的 owner 抽取发生新的职责或依赖方向变化；
- 更换 pinned upstream revision、board target 或 production composition 方式；
- 新增第二个可发布 firmware target，需要抽取共享 overlay/tooling。

## 关联

- [Firmware 架构](../architecture/firmware.md)
- [Release readiness](../quality/release-readiness.md)
- [交付仓决策](0001-delivery-repository.md)
