# 需求追踪矩阵

更新日期：2026-07-21
上游需求：[PRD](../product/prd.md)
发布状态：[Release readiness](release-readiness.md)

本矩阵只映射需求、权威设计和 required gate，不复制某次机器、网络、串口或 artifact 日志。

## 功能需求

| ID | 权威设计/协议 | Required gate |
| --- | --- | --- |
| FR-001 | `architecture/firmware.md` | Wi-Fi/NVS 配置、重启回读、失败恢复 HIL |
| FR-002 | `architecture/server.md` | 独立进程 Director/Worker bootstrap、binding selection 与 process E2E |
| FR-003 | `security/credentials.md` | grant tamper/replay/single-use 与 credential-origin tests |
| FR-004 | `protocol/rva-control-v1.md` | schema fixtures、双端 parser 与真实 WSS session |
| FR-005 | `protocol/wss-opus-v2.md` | 真实 provider process E2E、真机 ASR/TTS/playout 与 congestion |
| FR-006 | `protocol/udp-opus-gcm-v1.md` | canonical vectors、tamper/replay、真实 provider GCM process E2E、真机双向 UDP 和弱网 |
| FR-007 | `protocol/overview.md` | WSS/UDP profile selection、fresh reconnect 与 UI mode |
| FR-008 | `architecture/server.md` | provider fake、真实 provider stream 与错误隔离 |
| FR-009 | `architecture/firmware.md` | 中文字幕、状态、触摸开始/停止与 headless boundary |
| FR-010 | `protocol/lifecycle-errors.md` | exact cancel、stale generation、近讲/double-talk |
| FR-011 | `protocol/lifecycle-errors.md` | disconnect、Wi-Fi flap、bounded teardown/reconnect |
| FR-012 | `architecture/server.md` | shared admission、overload、queue limits |
| FR-013 | `architecture/server.md` | heartbeat、drain、lease revoke/release |
| FR-014 | `architecture/server.md` | provider adapter contracts、bulkheads 与 timeout |
| FR-015 | `architecture/server.md` | structured metrics、redaction 与 operator audit |

## 非功能需求

| ID | 权威设计/控制 | Required gate |
| --- | --- | --- |
| NFR-001 | `quality/test-strategy.md` | latency timeline、p95/p99 与固定实验环境 |
| NFR-002 | Server/Firmware ownership tables | overflow、cancellation、stack/queue/heap evidence |
| NFR-003 | `architecture/system.md` | single owner、fencing、stale callback fault injection |
| NFR-004 | ADR 0002 | Redis multi-Director/Worker、drain 与故障演练 |
| NFR-005 | `security/security-model.md` | TLS、UDP AEAD、replay、fuzz 与 source pin |
| NFR-006 | `security/credentials.md` | retention/redaction/secret scan 与轮换演练 |
| NFR-007 | AGENTS/architecture | import/dependency/repository contracts |
| NFR-008 | source locks/build | clean native build、size、artifact digest |
| NFR-009 | compatibility baseline | legacy/native affected-behavior parity |
| NFR-010 | deployment/runbooks | readiness、drain、rolling update 与 rollback |
| NFR-011 | capacity plan | CPU/RSS/event-loop/provider/queue pressure measurement |

## 更新规则

- 状态只在 [Release readiness](release-readiness.md) 更新；本矩阵不复制瞬时结果。
- 任何 wire 变更同时更新 schema/fixtures、协议文档和相关 required gate。
- `not_run` 不得由旧 artifact、host test 或设计文档替代。
- Waiver 必须包含 owner、风险、补测触发条件和过期条件。
