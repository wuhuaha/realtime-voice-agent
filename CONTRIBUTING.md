# 贡献指南

感谢贡献。Product 是当前协议、Server、ESP-IDF endpoint、正式测试和发布资产的唯一 authoring source；研究、失败路线、
原始评测和大型 artifact 请放在 Research 或受控存储，不要复制到 Product。

参与即表示同意遵守 [Code of Conduct](CODE_OF_CONDUCT.md)。一般使用问题先阅读 [Support](SUPPORT.md)；安全问题按
[Security policy](SECURITY.md) 私密报告。

## 开始前

- 先阅读 [README](README.md)、[协议总览](docs/protocol/overview.md)、[架构文档](docs/architecture/system.md) 和
  [测试策略](docs/quality/test-strategy.md)。
- 一个变更只修改一个主要边界；协议/schema、媒体热路径、firmware runtime 和 provider 行为需要先有决策或计划。
- 不提交 `.env`、Wi-Fi、token、API key、日志、音频、模型、生成配置、build 目录或固件 binary。

## 提交前

```bash
uv sync --locked --dev
uv sync --directory server --locked --all-packages --dev
uv sync --directory clients/desktop_reference --locked --extra test
uv run ruff check scripts tests firmware/tools
uv run pytest
uv run --directory server ruff check .
uv run --directory server pytest
uv run --directory clients/desktop_reference ruff check src tests
uv run --directory clients/desktop_reference pytest -m "not e2e_host"
uv run python scripts/verify_repository.py
uv run python scripts/check_secrets.py
git diff --check
```

需要 Redis、Docker、ESP-IDF、真实 provider、设备或公网环境的测试，必须在变更说明中写明实际命令和
`not_run`/证据等级，不能把 skipped 当作通过。

## Pull Request

- 描述问题、范围、非目标、验证命令和未完成风险。
- 协议变更必须同时更新 `protocol/`、consumer contract tests、版本/兼容说明和对应 ADR。
- 不在 Product 引入旧 wire、未登记媒体协议或业务专属 MCP/OTA/IoT 依赖。
- 提交保持单一意图，避免把重命名、格式化和无关清理混入实时路径变更。
