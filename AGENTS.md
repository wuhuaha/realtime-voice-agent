# AGENTS.md

本仓库是可交付的实时语音 Agent 产品仓。ESP32-S3 是首个 endpoint，但协议、服务端和测试边界不得
永久绑定单一设备类型。

## 工程原则

- 已验证行为优先于目录美观；协议、provider、runtime 或部署拓扑每次只改变一个主要边界。
- `protocol/` 是 wire contract 唯一 authoring source，Server 和 Firmware 不维护第二份常量。
- `session_director` 只负责 registry、capacity、lease/fencing、grant 和 drain，不接触媒体帧或 Agent turn。
- `realtime_worker` 是 active session、媒体 transport、AgentSession 和 playback generation 的唯一 owner。
- 媒体热路径不得访问 coordination store；共享 store 只用于注册、路由、grant 和 session 建立门禁。
- 生产多实例不得使用内存 coordination backend；它只允许测试和单进程开发。
- 固件保持 transport、audio、board、presentation 依赖方向；核心语音代码不得依赖 LVGL。
- 不迁移或恢复 Direct WebRTC、AIMP、PCM DataChannel 和研究仓归档实现。

## 安全

- 不提交 `.env`、`.env.local`、Wi-Fi、token、API key、生成配置头、音频、日志、模型或固件制品。
- grant 必须绑定 worker、device、session epoch、fencing token、profiles、expiry 和 jti，并单次消费。
- UDP 使用每 session 双向独立 AES-GCM key/salt；认证失败不得推进 sequence 或进入 decoder。
- 日志不得输出 token、密钥、完整设备凭据、原始音频或 provider 原始错误体。

## 语言与风格

- 文档和提交信息默认中文；标识符、schema、命令、库名、协议名和路径保留英文。
- Python 目标版本为 3.12；公开边界类型化，async 热路径不得直接执行阻塞 I/O。
- C/C++ 的 task、queue、timer、DMA 和 callback 必须有明确 owner、容量、停止和失败语义。

## 验证口径

使用 `unit_verified | contract_verified | host_verified | build_passed | image_sized | boot_observed |
device_verified | acoustic_verified | public_path_verified | measured | not_run`。构建通过不等于实机通过，旧固件
日志不能替代当前 artifact 的 HIL。

## Git

- 选择性暂存，提交保持单一意图；禁止把 external checkout、build、secret 或未知改动吞入提交。
- 默认不 push、tag、发布或创建外部资源，除非用户另行授权。
