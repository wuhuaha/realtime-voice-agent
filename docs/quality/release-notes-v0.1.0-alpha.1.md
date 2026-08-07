# v0.1.0-alpha.1 Release notes

状态：release candidate / not production-ready

`v0.1.0-alpha.1` 是首个公开技术预览后的增量候选。已发布的 `v0.1.0-alpha` annotated tag object为`0bc19f5`，
解引用后指向commit `6edf881`；该tag与既有release notes保持不可变，本轮使用新tag `v0.1.0-alpha.1`。

## 本轮增量

- 公共无凭据 ESP32-S3 bundle补齐`validate/flash/provision/erase-config`流程，并完成五镜像flash、NVS
  provisioning/readback、配置保留、擦除和WSS/UDP双协议真机回归。
- WSS endpoint启用独立TX lock，消除下行接收持锁时上行发送竞争；修复后的fresh firmware未再观察到对应锁失败或
  非预期fresh reconnect。
- 增加WSS/UDP容量、30分钟short-session churn和90-cell netem/netns弱网门禁；结果仅用于完成性和有界恢复验证，
  不提升为性能SLO或transport优劣声明。
- UDP真机continuous-operation运行约2小时18分钟，覆盖计划内freshness refresh、终点交互和normal close；启动早期
  的timestamp/stale finding已形成仅限UDP、无queue backlog且受预算约束的timeline recovery。
- Server runtime commit为`d5bace0e5246b9c92d5158cddc421c19a565078b`；证据文档successor为
  `47f6feea35b1ad5776e547fb4794f12a78e03429`。

## 验证身份

- GitHub Actions [run 31089931952](https://github.com/wuhuaha/realtime-voice-agent/actions/runs/31089931952)
  绑定`47f6fee`，repository、Server、Desktop Reference、双协议host E2E、Redis、native host contracts和
  ESP-IDF build/size共7个job全部成功。
- clean Server archive `rva-20260806T093500Z-d5bace0`由`d5bace0` Git archive部署，SHA-256为
  `ee892ffacaedcc3eb1242269951846b967a19a7e52c05b8b1ab5669bd450eaac`；readiness为200，UDP、provider和
  coordination均ready。
- firmware真机证据绑定`b03f706` fresh bundle及其digest；UDP timeline修复的真机无劣化回归绑定与`d5bace0`
  内容等价的临时release。clean `d5bace0` archive没有重复HIL，因此readiness不冒充精确archive的真机证据。
- 本轮不因docs、tag命名或内容等价archive重建而重复HIL。未来只有端侧、wire、媒体状态机、网络传输或硬件行为变化
  才触发针对性真机回归。

精确commit、artifact digest、测试数量和未运行门禁以[Release readiness](release-readiness.md)为准。

## 发布边界

本版本不是production-ready声明。UDP仍为显式opt-in；TLS、HA、入口限流、漏洞扫描、许可证复核、固定延迟SLO、
真机弱网、WSS 2小时、2小时short-session churn、24小时稳定性和真实容量测量仍不在已关闭门禁内。完整边界见
[Known limitations](known-limitations.md)。正式创建`v0.1.0-alpha.1`前，仍须从最终tag source重建发布artifact并校验
manifest、provenance、SBOM和digest；不得移动或覆盖`v0.1.0-alpha`。
