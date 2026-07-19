# 决策 0001：建立独立交付仓

日期：2026-07-20
状态：accepted
决定：使用 `realtime-voice-agent` 作为企业交付仓，ESP32-S3 是首个 endpoint；采用选择性提取、reference
复现和逐模块迁移，最终收口目标架构。

## 背景

原 `voice-agent-research` 同时包含研究、候选、实验、归档、外部源码和多条历史路线，不适合直接作为产品
发布、权限、安全扫描和长期维护边界。用户明确接受独立仓名与“先原样复现、后逐模块迁移”。

## 已考虑选项

### 继续使用研究仓

不选择。权威状态、归档路线、secret 风险和发布面无法清楚隔离。

### 过滤原 Git 历史

不作为默认。历史与工作树跨多路线，过滤仍可能携带失效资产，且不能准确表达当前选择性基线。

### 独立仓选择性提取

选择。通过 source manifest、固定 revision、patch、lock、behavior matrix 和 artifact hash 保持 provenance，
同时形成干净交付面。

### 一次性按目标目录重写

不选择。Wire、provider、runtime、firmware owner 和部署拓扑同时变化无法定位回退。

## 证据

- 研究仓 accepted 决策 `2026-07-19-realtime-voice-agent-delivery-repository-and-migration.md`。
- [兼容基线](../quality/compatibility-baseline.md)。
- `migration/baseline/source-manifest.yaml`、`behavior-matrix.yaml` 和 `third_party/sources.lock.yaml`。
- Server 迁移 commit `fca8de8`、repair commits `259aeee`/`d2fa0ca`；固件与仓库复现门禁 commit `cf9bc69`。
- 新仓根 pytest `15 passed`；Server Ruff 与 Redis-enabled pytest `179 passed`；reference firmware 在新仓
  clean build `2215/2215`。同一 final reference artifact 随后已在 COM11 完整烧录并观察启动、Wi-Fi、唤醒、
  WSS handshake、UDP probe 与持续 UDP 上行；真机语音闭环仍需独立门禁。

## 决定与范围

- 新仓是产品、协议、部署、测试和运维权威，不在运行时依赖研究仓。
- ESP32-S3 是首端，不是仓库永久边界。
- 迁移顺序为 reference materialization -> compatibility guard -> 单 owner 模块迁移 -> 目标目录收口。
- Direct WebRTC、AIMP、PCM DataChannel 和研究归档实现不迁移。
- HTML 暂不创建；Markdown/schema/fixtures/实现稳定后只生成 non-normative 可视化。
- 已验证旧功能优先；每批只改变一个主要边界，并运行匹配的四象限/HIL。

## 后果与风险

正面：交付面、安全、依赖、许可证和发布 ownership 清晰；未来 endpoint 可扩展。代价：迁移期 reference 与
target lane 并存，存在短期重复和更多兼容测试。风险：目录看似完整但 HIL 未完成；必须用证据等级阻止
“文件已迁移”等同“产品可用”。

## 兼容和迁移

Reference lane 在目标 lane 通过等价门禁前保持可构建；最终发布不依赖研究仓或 external checkout常驻。
Provenance、fixtures 和 baseline 永久保留。新仓 host synthetic Chinese 已完成真实 provider media E2E，但该
host 证据不能替代 ESP32 acoustic E2E；设备目前只证明 boot、wake、WSS handshake、UDP transport 和持续 UDP 上行，尚未触发
真机 ASR。

## 复查触发条件

- 新增第二类真实 endpoint，现有协议/包边界无法无分支接入。
- Target firmware 抽取要求关闭 AEC/UI/WDT 或显著改变资源余量。
- 四象限表明兼容层成本高于明确提升 protocol version。
- 交付仓重新混入研究候选、归档或外部源码整仓。

## 关联链接

- [PRD](../product/prd.md)
- [系统架构](../architecture/system.md)
- [测试策略](../quality/test-strategy.md)
