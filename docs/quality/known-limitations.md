# Known limitations

适用版本：`v0.1.0-alpha.1`

本版本用于验证低资源 endpoint 通过 RVA wire 接入 roomless LiveKit Agents 的工程边界，不是 production-ready、
通用 RTC 或完整语音产品声明。精确门禁状态以 [Release readiness](release-readiness.md) 为准。

## 部署与安全边界

- Server 运行入口只支持 Linux/container；Windows 只作为开发、固件构建和 host test 环境。
- `deployment/single-node/` 不提供 TLS、证书、入口限流、WAF、Redis HA 或多主机故障转移。公网部署必须在仓库外配置
  受信 HTTPS/WSS gateway；UDP 需单独配置防火墙、NAT 和暴露端口。
- 单机 Compose 是可复现部署基线，不是高可用拓扑。Redis、主机或唯一 Worker 故障会影响新 session 建立。
- 示例配置只含占位符。设备 provisioning、secret rotation、撤销、审计和生产证书生命周期由部署方负责。
- 公共 bundle 工具可避免把凭据放入 firmware image、argv、日志或 Git，并在 NVS 写入后执行 readback digest校验；
  当前 reference firmware 未启用 NVS encryption，物理 flash读取仍可恢复已 provisioned credential。临时文件删除也
  不等于 SSD 安全擦除。

## Endpoint 与协议边界

- 当前 reference endpoints 只有立创实战派 ESP32-S3 和 Python Desktop Reference Client；浏览器、手机及其他 MCU
  尚无本仓实现和发布门禁。
- WSS `wss-opus/1` 是 baseline。UDP `udp-opus-gcm/1` 是显式启用的 challenger，在完成固定弱网门禁前不应作为
  全量默认或隐式自动选择。
- UDP 不实现 ICE、TURN、RTCP、NACK/RTX、GCC、无缝 NAT rebinding 或 transport 热迁移；失败后采用 fresh session。
- 协议只承诺 `rva/1`、`wss-opus/1` 和 `udp-opus-gcm/1`，不兼容未登记的历史 wire 或 media profile。
- WSS Server media timeline从首个已接收 packet 建立。当前 wire 不携带可跨主机验证的 capture phase，因此无法单独
  识别“首包在 TCP 中已滞留超过 freshness budget”的情况；端侧 pre-send gate/send timeout、后续 packet cadence、
  Server queue age 和 fresh-session fail closed共同缩小影响，但不构成首包端到端年龄证明。

## 尚未形成发布承诺的指标

- 早期8-cell、1-repeat netem结果只是已被扩展矩阵取代的小样本。修复后的90-cell全量重跑为83次完整
  `completed`、2次严格`bounded_recovery_verified`和5次预期UDP blocked，各scenario/profile组均为5/5。loss场景
  只有完整`completed`，或同时证明playback stopped、fresh reopen、
  旧媒体隔离和资源清理的`bounded_recovery_verified`才可接受；non-loss场景不得用恢复替代成功。目标`tc`不支持seed，
  `paired_randomness=false`；逻辑pair不能解释为相同随机序列，也不能据此声称WSS/UDP性能优劣。
- Host 已验证 1/5 并发的 WSS/UDP 短 session容量阶梯及完整资源回收。当前发布runtime的远端独立30分钟
  short-session churn为WSS `18013/18013`、UDP `17855/17855`，最终active session均为0且进程/端口回收通过。
  UDP真机已完成约2小时18分钟continuous-operation soak，覆盖计划内freshness换钥、终点交互和normal close；开始约
  6分钟处出现两次timestamp拒绝、两次stale-media熔断和一次handshake timeout，均通过fresh reopen有界恢复，随后约
  132分钟仅发生计划内refresh。后续修复已用确定性测试复现timestamp/stale边界，并实现仅UDP、queue无backlog、
  最大两个freshness window、10秒最多两次的有界reanchor/recovery；完整Server/firmware host门禁和一轮真机无劣化回归
  通过，但尚未以clean Product commit重复长稳并自然命中该分支。因此原结果仍不是“单session零重连”声明。
  WSS 2小时、2小时short-session churn、
  24小时和真实容量仍为`not_run`；1/5启动值也不提供容量SLO。
- AEC、NS、VAD、远场拾音、double-talk 和 ASR 中文准确率没有形成跨环境声学指标；当前真机证据只证明端云链路，
  不代表特定噪声环境中的识别或主观音质。
- `VOICE_WORKER_MAX_SESSIONS=5` 是可配置启动值，不是测量容量。

## Provider 与分发边界

- FunASR、DeepSeek-compatible LLM 和 CosyVoice/MiMo adapters 是 reference integrations。第三方服务可用性、模型质量、
  计费、数据处理和服务条款不由本项目保证。
- Desktop Reference Client 是协议和 E2E 工具，不提供签名安装器、自动更新或面向终端用户的桌面发行版。
- Release SBOM 是锁定组件清单，不是漏洞扫描结论。字体、Python native wheel、FFmpeg/PyAV、PortAudio 等依赖仍须由
  实际二进制发布方按目标平台复核许可证和分发义务。

上述限制不影响已登记 wire、资源有界生命周期、WSS/UDP 真机闭环和 Linux 单节点部署基线的验证结论。公共无凭据
firmware bundle已完成可复现构建、size、provenance、五镜像flash、临时NVS provisioning/readback、配置保留、
erase-config和WSS/UDP真机门禁；bundle不携带可直接联网的凭据，部署方仍须提供安全provisioning。最终 release source、
tag、firmware provenance 和 CI 均绑定`047487b`。UDP继续保持显式opt-in；90-cell host矩阵通过不替代真机弱网、
物理播放或性能SLO。
