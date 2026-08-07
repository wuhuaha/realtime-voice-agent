## 问题与范围

说明要解决的问题、变更范围和明确非目标。

## 行为与边界

说明是否影响 wire/schema、Server lifecycle、provider、Firmware 热路径、资源容量、部署或安全边界。

## 验证

列出实际执行的命令和结果。未运行项必须写为`not_run`并说明原因。

- [ ] 变更保持单一意图，没有包含凭据、日志、音频、模型或生成制品
- [ ] 已运行与影响面匹配的 lint、test、build 或 HIL
- [ ] 协议变更同步更新`protocol/`、consumer tests、文档和 ADR
- [ ] Firmware 变更说明 heap/stack、task/queue ownership 和真机验证范围
- [ ] 新增依赖已检查来源、版本、许可证和发布影响
- [ ] 文档和 Known limitations 与实际状态一致

## 剩余风险

列出仍未验证的边界、回滚方式和后续工作。
