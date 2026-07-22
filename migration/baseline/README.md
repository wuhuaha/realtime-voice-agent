# 迁移基线

本目录只保留建立 Product 仓时的最小、只读来源与行为 provenance，不保存运行日志或固件制品编年。它不是
当前架构、构建输入或开发入口；主线代码和发布流程不得依赖这里的历史叙述。

- `source-manifest.yaml`：来源 commits、tree object、上游 revision、锁文件与选定文件 hash；目录提升后的文件用
  `source_path` 保留历史位置，用 `production_path` 指向当前校验位置。
- `behavior-matrix.yaml`：功能行为及证据等级。

构建 hash、端口、网络和单次 HIL 观察属于运行证据，不进入该基线。发布状态统一见
`docs/quality/release-readiness.md`。
