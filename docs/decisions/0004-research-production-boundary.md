# 决策 0004：Product 与 Research 采用单向提升边界

日期：2026-07-21
状态：accepted

## 背景

本仓是 `realtime-voice-agent` 的产品、协议、构建、部署和发布权威。外部 Research 仓需要基于当前产品代码做
候选验证和失败实验，但不能让产品运行依赖 Research，也不能通过双向复制形成第二份 Product source。

## 决定

- 本仓是 Server、Firmware、current wire schema/fixtures、正式测试和交付文档的唯一 authoring source。
- Research 通过本仓 Git worktree branch 修改实验性产品代码；Product source不复制到 Research tracked tree。
- 实验 branch 只有在用户接受、删除一次性 instrumentation并通过 Product验证后才能进入 main/release。
- 本仓 CI、build、runtime和deployment不得读取 `voice-agent-research` 或 `realtime-voice-agent-research` 路径。
- 跨仓证据使用 experiment id、Research/Product commit和artifact digest关联；manifest不是运行配置。
- Product当前协议和架构不在Research二次authoring。候选转正时，在本仓写结论版ADR/schema/test。

## 后果

本仓保持独立可交付，Research 可以保留失败路线和内部证据。代价是跨仓工作不能依赖单个原子 commit，必须
通过 manifest 和 CI/HIL identity 建立可追溯关系。

## 复查条件

- 出现多个独立产品消费者并需要稳定 shared package。
- Product 构建或运行开始依赖 Research 路径。
- worktree/manifest 的持续成本有证据高于 monorepo 隔离成本。

## 关联

- [交付仓边界](0001-delivery-repository.md)
- [生产固件 composition](0003-production-firmware-composition.md)
