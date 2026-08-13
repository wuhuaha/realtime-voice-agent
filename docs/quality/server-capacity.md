# Provider-free Server capacity

更新日期：2026-08-13

状态：`not_run / no public reproducible baseline`

当前公开仓提供 provider-free 容量工具，用于在不调用 ASR、LLM、TTS 和 LiveKit Agent runtime 的条件下验证
Director、Worker、WSS/UDP media、playback facts、route release 和资源回收。工具入口为：

- `server/tools/capacity_soak.py`
- `server/tools/capacity_sharded.py`
- `server/tools/capacity_cluster.py`

这些工具及其确定性测试是当前唯一受维护入口，但工具存在和测试通过不等于形成了容量测量结果。

## 当前证据状态

当前没有从公开、可恢复 Product commit 运行并带有公开脱敏 summary/environment manifest 的 provider-free 容量基线。
早期内部实验依赖未提交的 harness 快照和未公开 artifact，公开读者无法从其 base commit 精确重建，因此不再把其中的
并发数、资源下限、容量阶梯、带宽或 `pids_limit` 数值作为 `measured` 结果、配置建议或发布门禁。

`VOICE_WORKER_MAX_SESSIONS=5` 只是可配置启动默认值，不是容量测量、SLO 或目标环境推荐值。部署方在获得新的公开基线前，
必须在自身 provider、网络、TLS/代理和资源约束下保守配置并复测。

## 形成新基线的门禁

后续只有同时满足以下条件，才能在本页发布 `measured` 容量结论：

1. 从公开可获取、无未提交 harness 改动的精确 Product commit 运行。
2. 记录锁文件、容器镜像、命令、场景、持续时间、admission、主机与 cgroup/network 配置。
3. 保存逐 cell 状态、成功判据、失败分类、资源采样和最终 session/进程/端口回收事实。
4. 发布脱敏 summary、environment manifest、source identity 和可访问 artifact 的校验信息。
5. 区分 provider-free 接入层结果与包含真实 ASR/LLM/TTS/Agent runtime 的端到端结果。
6. 明确公网 TLS、反向代理、NAT、丢包抖动、真实 endpoint 和背景负载等适用边界。

在这些条件完成前，容量状态保持 `not_run`，不得用私有 artifact、不可下载 digest、历史观察或 harness 单元测试替代。
