# 需求追踪矩阵

快照日期：2026-07-20
上游需求：[PRD](../product/prd.md)

## 1. 功能需求

| ID | 设计/协议 | 主要验证 | 当前证据 |
| --- | --- | --- | --- |
| FR-001 | `architecture/firmware.md` | firmware UI/NVS HIL、重启回读 | boot/Wi-Fi observed；UI/触摸/配置回读 `not_run` |
| FR-002 | `architecture/server.md` | Director bootstrap contract + process e2e | 真实进程 bootstrap + grant `host_verified` |
| FR-003 | `security/credentials.md` | grant unit/Redis single-use/tamper tests | shared 原子消费、tamper/replay/Redis 重建与跨实例 `unit_verified` |
| FR-004 | `protocol/xiaozhi-control-v1.md` | schema fixtures、WSS host/HIL | device WSS handshake 约 20 ms；业务 control HIL 未完成 |
| FR-005 | `protocol/wss-opus-v1.md` | deterministic host + real-provider HIL | host provider E2E；device 持续 uplink；真机 ASR/downlink `not_run` |
| FR-006 | `protocol/udp-opus-gcm-v1.md` | cross-language fixtures、UDP host/HIL | device GCM probe ready + 600+ uplink；真机 ASR/downlink `not_run` |
| FR-007 | `protocol/overview.md` | hello negotiation、UI toggle、restart | WSS/UDP device paths observed；UI toggle/restart `not_run` |
| FR-008 | `architecture/server.md` | provider fake、real-provider smoke | host FunASR/DeepSeek/CosyVoice media E2E；ESP32 acoustic E2E `not_run` |
| FR-009 | `architecture/firmware.md` | subtitle state fixture + display HIL | display initialized；UI/字幕人工验收 `not_run` |
| FR-010 | `protocol/lifecycle-errors.md` | abort race、generation、double-talk HIL | host/source contract；最终 acoustic `not_run` |
| FR-011 | `protocol/lifecycle-errors.md` | disconnect/restart/network flap e2e/HIL | `not_run` |
| FR-012 | `architecture/server.md` | Worker admission boundary/overload | admission/capacity `unit_verified`；容量压测 `not_run` |
| FR-013 | `architecture/server.md` | heartbeat/drain/deadline e2e | Redis heartbeat/drain 并发 `unit_verified`；部署演练 `not_run` |
| FR-014 | `architecture/server.md` | provider adapter contract/fakes | Server test suite 覆盖 adapters/fakes；real-provider audio `not_run` |
| FR-015 | `architecture/server.md` | structured event/redaction contract | observability/redaction tests 已纳入 `179 passed`；运维审计 `not_run` |

## 2. 非功能需求

| ID | 设计/控制 | 主要验证 | 当前证据 |
| --- | --- | --- | --- |
| NFR-001 | test strategy weak-network/acoustic | latency timeline + p95/p99 | `not_run` |
| NFR-002 | Server/Firmware ownership tables | overflow/cancellation/stack/queue | 部分 host/source contract；HIL `not_run` |
| NFR-003 | System invariant #1 | lease race + session registry + stale generation | Redis lease/fencing/跨实例 `unit_verified`；真实故障演练 `not_run` |
| NFR-004 | ADR-0002 | Redis multi-Director/Worker e2e | shared coordination `unit_verified`；真实多进程容量 `not_run` |
| NFR-005 | security model + UDP spec | auth/replay/fuzz/TLS/public path | fixture contract；public path `not_run` |
| NFR-006 | security credentials/model | retention/redaction/log scan | repository rules defined；operational audit `not_run` |
| NFR-007 | architecture docs/AGENTS | import/dependency architecture tests | repository contract partial |
| NFR-008 | source lock/build scripts | clean checkout build + artifact digest | 新仓 `build_passed/image_sized`，SHA-256 `43bac4d4ed678b3298cc9f4c8e9da0c4ab7608af731406cec31939ee457350c8` |
| NFR-009 | compatibility baseline | reference/new four quadrants | `not_run` |
| NFR-010 | deployment/runbooks | health/drain/rollback exercise | launcher stop/start `host_verified`；drain/rollback exercise `not_run` |
| NFR-011 | test strategy capacity | CPU/RSS/event-loop/provider pressure | `not_run` |

## 3. 发布时更新规则

- 每项必须指向实际 test id、CI run 或 evidence 文件，不能只链接设计文档。
- `not_run` 只能变更为项目证据词汇中的状态，并记录环境、artifact、命令和原始结果。
- 旧 baseline 不升级为新组合的 `device_verified`。
- Waiver 必须包含 owner、风险、补测触发条件和过期条件。
- 任何 wire 变更同时更新 schema/fixtures、协议文档和本矩阵。
