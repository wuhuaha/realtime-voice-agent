# Release readiness

更新日期：2026-08-07

状态：`v0.1.0-alpha.1` 已发布 / 技术预览 / 非生产就绪

本文只记录当前公开版本的可复核身份、门禁结果和限制。实验过程、临时地址、SSID、原始串口日志与已经被取代的
artifact 不属于 Product 发布文档；需要追溯时使用 Git 历史和对应 CI/Release 记录。

## 发布身份

| 项目 | 最终身份 |
| --- | --- |
| Source、tag、firmware provenance | `047487b92669af6570c1e1d5c86085ce59e42504` |
| GitHub Release | [`v0.1.0-alpha.1`](https://github.com/wuhuaha/realtime-voice-agent/releases/tag/v0.1.0-alpha.1) |
| GitHub Actions | [run `31140253810`](https://github.com/wuhuaha/realtime-voice-agent/actions/runs/31140253810)，7 个 job 全部成功 |
| Firmware bundle | `rva-firmware-public-v0.1.0-alpha.1.zip`，SHA-256 `7ae8481a5fa8e6dceb9822f09552e48e31a1e087707c5052294eee2957c4afeb` |
| Firmware application | `rva_voice_terminal.bin`，SHA-256 `c979167e4fe16ade7332da0b89b55b77df24e879a7ee6ec40a84fa7643a6d661` |
| Firmware provenance | SHA-256 `8cfff27be16b938e7ceb67fdb2c068cff5a95082c8d29afd493cd2326976bbd3` |
| CycloneDX SBOM | SHA-256 `10d3e4729fec9b2e5a50dea7f1bd38c4fc2fc9c66134c1645f302078ea70f519` |
| Linux validation archive | `rva-20260807T020334Z-047487b`，SHA-256 `68bd9482fd4a6fd104f8f15bf002057e4fceef746da706c230dec0de841d3b72` |

GitHub Release 中的 `SHA256SUMS-v0.1.0-alpha.1.txt` 覆盖 bundle、provenance 和 SBOM。Bundle 内的
`manifest.json`、`SHA256SUMS` 与独立 provenance 共同绑定五个烧录镜像、分区 offset、公共空凭据配置、锁定依赖和
字体来源。发布附件不包含 Wi-Fi、bootstrap credential 或 provider secret。

## 已验证门禁

| Gate | 状态 | 证据边界 |
| --- | --- | --- |
| Repository、secret、schema | `host_verified` | repository contracts、secret scan、protocol fixtures 通过 |
| Server unit/contract/integration | `host_verified` | Director、Worker、provider fake、failure/lifecycle 测试通过 |
| Desktop Reference | `host_verified` | unit、WSS/UDP deterministic process E2E 通过 |
| Redis horizontal coordination | `host_verified` | lease/fencing、capacity、drain、进程和端口回收通过 |
| Native firmware host contracts | `host_verified` | config、audio、UI reducer、WSS/UDP 和 lifecycle contracts 通过 |
| Native clean build + size | `build_passed / image_sized` | 锁定 ESP-IDF 5.5.2；4 MiB app partition 余量约 47% |
| Public firmware packaging | `host_verified` | manifest、provenance、许可证、五镜像和 provisioning CLI 校验通过 |
| WSS voice loop | `device_verified` | bootstrap、双向 Opus、字幕、完整播放、normal close 和 exact release 通过 |
| UDP voice loop | `device_verified` | authenticated probe、source pinning、双向 Opus、generation 和 exact release 通过 |
| Provisioning | `device_verified` | flash、NVS provision/readback、配置保留和 erase-config 通过 |
| Weak-network completion matrix | `host_verified / performance incomplete` | 90 次受控场景完成性通过；不构成 WSS/UDP 性能优劣或延迟 SLO |
| 30 分钟 short-session churn | `host_verified` | WSS `18013/18013`、UDP `17855/17855`；最终 active session 为 0 |
| UDP continuous operation | `device_verified / limited` | 约 2 小时 18 分钟，包含计划内 freshness refresh 并正常关闭 |
| Linux single-node readiness | `public_path_verified` | Director、Worker、Redis、provider/UDP readiness 与 rollback smoke 通过 |

设备验证绑定的 firmware runtime 在最终 tag 前没有行为变化；最终 release bundle 从 clean `047487b` 重新构建并绑定
相同 runtime。仅有文档、tag identity 和重新打包变化时不机械重复 HIL；端侧、wire、媒体状态机、transport 或硬件行为
变化时必须执行针对性真机回归。

## 发布后实验

基于后续 Product commit `f53997b137c5fcbd97568c516e6c1a658679a78c` 与精确实验快照的 provider-free
容量矩阵，已验证 WSS/UDP 100 并发在单 Worker `2 vCPU / 1 GiB` 下的 steady 与 5 分钟 churn，并在
`2 vCPU / 2 GiB` 下通过 130 并发 10 分钟余量验证。该结果不属于 `v0.1.0-alpha.1` tag 的发布门禁；实验镜像
身份、资源数据和有限容量阶梯见 [Server capacity](server-capacity.md)；更高容量仍未测量。

## 未关闭门禁

- Provider-free 10/100 并发已有资源基线，但本版本仍不承诺真实 Agent 的端到端 p50/p95/p99 latency、最大并发或
  容量 SLO；详见 [Server capacity](server-capacity.md)。
- 真机弱网、WSS 两小时、两小时 short-session churn 和 24 小时稳定性未形成发布证据。
- TLS termination、WAF、入口限流、Redis HA、多主机故障转移和 secret lifecycle 不由单机 Compose 提供。
- AEC、NS、VAD、远场、double-talk、中文 ASR 准确率和主观音质不属于本次协议接入技术预览的发布结论。
- SBOM 是锁定组件清单，不是漏洞扫描或完整许可证合规审计。
- ESP32 配置 NVS 当前未加密，不能抵御物理 flash 读取。

完整适用边界见 [Known limitations](known-limitations.md)。上述未关闭项不得由旧 artifact、host test、构建成功或
主观体验替代。
