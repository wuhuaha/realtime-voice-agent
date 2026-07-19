# 已知并发债务

本文件只记录当前 overlay 最终源码中仍存在、但未被本轮生命周期修复关闭的边界。

- `Protocol::error_occurred_` 和 `Protocol::last_incoming_time_` 仍可能由网络回调写、由应用任务读；本轮没有建立统一状态 owner，也不声称已消除这两处 data race。
- WebSocket owner 已按 `connection_generation` 受 `control_owner_mutex_` 保护，解决的是旧 session 消息误发到新 owner 和指针生命周期问题；底层 `WebSocket::Send`、`IsConnected` 与断开回调之间的库内线程安全仍依赖 `esp-ml307` 契约。
- generation 校验后已经进入底层的单次 WebSocket 或 UDP `Send` 无法被无阻塞撤销。teardown 会阻止后续发送、重试和跨 generation owner 复用，但不宣称取消已进入驱动的 datagram/frame。
- WebSocket v2/v3 binary frame 的长度校验和只读 buffer 解码仍沿用 upstream 实现，尚未纳入本轮 session lifecycle 范围。

关闭这些债务前，需要先定义 callback/task owner、底层网络 API 的同步契约和相应 host/target 并发测试，不应只增加零散原子变量。
