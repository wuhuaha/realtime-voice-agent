# v0.1.0-alpha.1 Release notes

状态：已发布 prerelease / not production-ready

`v0.1.0-alpha.1` 是首个公开技术预览后的增量版本，已于 2026-08-07 发布。最终 source、tag 与 firmware
provenance 均绑定 commit `047487b92669af6570c1e1d5c86085ce59e42504`。

## 本轮增量

- 公共无凭据 ESP32-S3 bundle补齐`validate/flash/provision/erase-config`流程，并完成五镜像flash、NVS
  provisioning/readback、配置保留、擦除和WSS/UDP双协议真机回归。
- WSS endpoint启用独立TX lock，消除下行接收持锁时上行发送竞争；修复后的fresh firmware未再观察到对应锁失败或
  非预期fresh reconnect。
- 增加WSS/UDP容量、30分钟short-session churn和90-cell netem/netns弱网门禁；结果仅用于完成性和有界恢复验证，
  不提升为性能SLO或transport优劣声明。
- UDP真机continuous-operation运行约2小时18分钟，覆盖计划内freshness refresh、终点交互和normal close；启动早期
  的timestamp/stale finding已形成仅限UDP、无queue backlog且受预算约束的timeline recovery。
- 从最终 clean tag source 生成 firmware bundle、provenance、CycloneDX SBOM 和 SHA-256 清单。

## 验证身份

- GitHub Actions [run 31140253810](https://github.com/wuhuaha/realtime-voice-agent/actions/runs/31140253810)
  绑定`047487b`，repository、Server、Desktop Reference、双协议host E2E、Redis、native host contracts和
  ESP-IDF build/size共7个job全部成功。
- clean Server archive `rva-20260807T020334Z-047487b` SHA-256为
  `68bd9482fd4a6fd104f8f15bf002057e4fceef746da706c230dec0de841d3b72`；readiness为200，UDP、provider和
  coordination均ready。
- 最终 firmware bundle SHA-256 为
  `7ae8481a5fa8e6dceb9822f09552e48e31a1e087707c5052294eee2957c4afeb`，manifest source revision 为`047487b`。
- Firmware runtime 在最终 tag 前没有行为变化，因此不因文档和发布身份整理重复 HIL；未来端侧、wire、媒体状态机、
  transport 或硬件行为变化时执行针对性真机回归。

精确commit、artifact digest、测试数量和未运行门禁以[Release readiness](release-readiness.md)为准。

## 发布边界

本版本不是production-ready声明。UDP仍为显式opt-in；TLS、HA、入口限流、漏洞扫描、许可证复核、固定延迟SLO、
真机弱网、WSS 2小时、2小时short-session churn、24小时稳定性和真实容量测量仍不在已关闭门禁内。完整边界见
[Known limitations](known-limitations.md)。发布附件、校验和与下载入口见
[GitHub Release](https://github.com/wuhuaha/realtime-voice-agent/releases/tag/v0.1.0-alpha.1)。
