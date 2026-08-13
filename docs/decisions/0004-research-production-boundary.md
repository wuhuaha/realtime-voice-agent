# 决策 0004：Product 与 Research 采用单向提升边界

日期：2026-08-01
状态：accepted

## 决定

- Product 是 Server、Firmware、current wire schema/fixtures、正式测试、部署和发布文档的唯一 authoring source。
- Research 是仅限维护者访问的私有工作台，保存候选方案、失败路线、实验规格、原始结果和历史背景；它不是公共贡献面，
  也不得成为 Product 的运行时、构建或部署依赖。
- 外部贡献者统一通过 Product 的公开 Issue/PR 提交研究建议和实验提案，不需要访问 Research；维护者负责在进入内部流程前
  去除凭据、个人信息、私有地址和其他不应传播的内容。
- Research 通过 Product 的 Git worktree branch 进行实验；禁止在两个仓库复制或二次维护同一份 Product source/schema。
- 候选只有在用户接受、移除一次性 instrumentation、通过 Product 验证并完成来源/许可证与公开性检查后，才能把
  结论版 ADR/schema/test 提升到 Product；私有讨论和原始材料不随之公开。
- 跨仓证据使用 experiment id、Product/Research commit 和 artifact digest 关联；manifest不是运行配置，也不是协议输入或
  运行时依赖。
- Product CI、build、runtime 和 deployment 不得读取 `voice-agent-research`、`realtime-voice-agent-research` 或
  其他本地 Research 路径。

## 后果

Product 可以从 clean checkout 独立构建、测试、部署和发布，公共贡献者也能只凭 Product 中的材料完成参与和复核；
Research 可以保留私有失败证据而不污染交付面。跨仓工作不能依赖一个原子提交，必须记录可复核的
commit/artifact identity 和真实验证状态。Product 中的公开结论不得以访问私有 Research 为成立前提。

## 复查条件

- 出现多个独立产品消费者并需要稳定共享 package。
- Product 构建或运行开始依赖 Research 路径。
- worktree/manifest 的持续成本有证据高于仓库隔离成本。

## 关联

- [当前协议与架构](../index.md)
- 维护者私有研究记录（不公开链接，也不作为 Product runtime dependency）
