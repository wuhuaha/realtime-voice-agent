# Product 文档导航

本文档树描述当前 Product source、wire、部署和开发流程。machine-readable contract 是 wire 权威；ADR 是架构
决策权威；Markdown 是开发和运维说明；HTML 手册只作非规范性图解。

- [端云开发手册（HTML）](guides/realtime-voice-agent-developer-guide.html)

## 架构与决定

- [系统架构](architecture/system.md)
- [Server 架构](architecture/server.md)
- [Firmware 架构](architecture/firmware.md)
- [Native ESP-IDF 与 RVA 决策](decisions/0005-native-esp-idf-endpoint-and-rva-protocol.md)
- [服务端打断裁决与 RVA v2 决策](decisions/0006-server-authoritative-interruption-and-rva-v2.md)
- [Python Desktop Reference Client 决策](decisions/0007-python-desktop-reference-client.md)

## 协议与开发

- [协议总览](protocol/overview.md)
- [RVA Control v2](protocol/rva-control-v2.md)
- [WSS Opus v3](protocol/wss-opus-v3.md)
- [UDP Opus GCM v2](protocol/udp-opus-gcm-v2.md)
- [生命周期与错误](protocol/lifecycle-errors.md)
- [本地开发](operations/local-development.md)
- [部署](operations/deployment.md)
- [故障排查](operations/troubleshooting.md)

## 质量与安全

- [测试策略](quality/test-strategy.md)
- [ESP32 端侧稳定性优化方案与实施计划](quality/esp32-runtime-stability-plan.md)
- [Release readiness](quality/release-readiness.md)
- [需求追踪](quality/requirements-traceability.md)
- [安全模型](security/security-model.md)
- [凭据](security/credentials.md)
