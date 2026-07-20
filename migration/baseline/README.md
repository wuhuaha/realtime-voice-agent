# 迁移基线

本目录固定从 `voice-agent-research` 选择性提取的来源、行为和制品证据。它不使新仓在运行时依赖研究仓。

- `source-manifest.yaml`：来源 commits、tree object、上游 revision、锁文件与选定文件 hash；目录提升后的文件用
  `source_path` 保留历史位置，用 `production_path` 指向当前校验位置。
- `behavior-matrix.yaml`：功能行为及证据等级。
- `artifacts.sha256`：按时间保留已验证 artifact 的 hash；末尾 `final-graceful-stop-*` 是当前评测 app，
  `final-ui-control-lifecycle-*`、`ui-control-responsiveness-*`、`public-director-*` 及之前条目均为历史
  predecessor。固件可能包含部署 secret，
  不作为公开发布制品提交。

不同 clean build 曾生成不同固件 hash，因此记录的 fresh artifact 只证明指定环境构建，不声明 bit-level
reproducibility。
