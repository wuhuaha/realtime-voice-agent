# Product 文档导航

本文档树描述当前 Product source、wire、部署和开发流程。machine-readable contract 是 wire 权威；ADR 是架构
决策权威；Markdown 是开发和运维说明；HTML 手册只作非规范性图解。

- [端云开发手册（HTML）](guides/realtime-voice-agent-developer-guide.html)

## 架构与决定

- [系统架构](architecture/system.md)
- [Server 架构](architecture/server.md)
- [Firmware 架构](architecture/firmware.md)
- [Native ESP-IDF 与 RVA 决策](decisions/0005-native-esp-idf-endpoint-and-rva-protocol.md)
- [服务端打断裁决与 RVA Protocol 1.0 决策](decisions/0006-server-authoritative-interruption-and-rva-v1.md)
- [Python Desktop Reference Client 决策](decisions/0007-python-desktop-reference-client.md)

## 协议与开发

- [协议总览](protocol/overview.md)
- [RVA Protocol 1.0](protocol/rva-protocol-v1.md)
- [WSS Opus 1](protocol/wss-opus-v1.md)
- [UDP Opus GCM 1](protocol/udp-opus-gcm-v1.md)
- [生命周期与错误](protocol/lifecycle-errors.md)
- [本地开发](operations/local-development.md)
- [部署](operations/deployment.md)
- [故障排查](operations/troubleshooting.md)

## 质量与安全

- [测试策略](quality/test-strategy.md)
- [Release readiness](quality/release-readiness.md)
- [v0.1.0-alpha.1 Release notes](quality/release-notes-v0.1.0-alpha.1.md)
- [Known limitations](quality/known-limitations.md)
- [需求追踪](quality/requirements-traceability.md)
- [安全模型](security/security-model.md)
- [凭据](security/credentials.md)

仓库级贡献、安全报告和第三方许可证入口：[`CONTRIBUTING.md`](../CONTRIBUTING.md)、[`SECURITY.md`](../SECURITY.md)、
[`LICENSE`](../LICENSE)、[`NOTICE`](../NOTICE)。
