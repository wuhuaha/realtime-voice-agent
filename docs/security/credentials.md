# 凭据管理

更新日期：2026-07-20

## 1. 原则

- 真实值只来自 secret manager 或 ignored local env，不提交、不写镜像、不放命令行。
- Template 只保存变量名和无效占位符。
- 不在日志、异常、截图、测试 fixture、artifact manifest 或聊天记录重复凭据。
- 每类 secret 有独立用途、owner、rotation 和 revoke 路径，不复用同一个 token。

## 2. Server 凭据清单

| Variable | Consumer | 用途 | 生产要求 |
| --- | --- | --- | --- |
| `VOICE_INTERNAL_TOKEN` | Director/Worker | heartbeat、drain internal API | 高熵、私网、可轮换；优先 mTLS/identity proxy |
| `VOICE_GRANT_SIGNING_KEY` | Director/Worker | 签发/验证 connect grant | 至少 16 bytes，建议 32 random bytes；版本化双读轮换 |
| `VOICE_LAB_TOKEN` | Worker | 明确开发直连 | 生产禁用或隔离限制 |
| `VOICE_DEVICE_BOOTSTRAP_TOKEN` | Director | 设备 bootstrap | 当前共享 token 是缺口；目标分设备/enrollment |
| `VOICE_LLM_API_KEY` | Worker | LLM provider | Provider scoped、费用/速率限制 |
| `VOICE_MIMO_API_KEY` | Worker | 可选 TTS | 仅选择 MiMo 时配置 |
| Redis credential in `VOICE_REDIS_URL` | Director | coordination | secret manager 注入，日志只显示 host/db |

当前 FunASR 与 remote CosyVoice adapter 使用 endpoint 配置，没有独立 API-key 字段；需要鉴权时应通过受控
gateway/endpoint policy，不能把未被 Settings 消费的自造变量当作已生效凭据。

`.env.example` 是当前默认 topology/provider 的字段形状，已经列出 `VOICE_DEVICE_BOOTSTRAP_TOKEN`，但
`VOICE_MIMO_API_KEY` 等未选 provider 的可选字段不保证全部展开。最终以 typed Settings 和部署 chart 的交集
校验；生产缺失、placeholder 或不安全的 shared auth 必须 fail fast。

## 3. Firmware 本地配置

| Kconfig/local input | 说明 |
| --- | --- |
| `RVA_DIRECTOR_BOOTSTRAP_URL` | Director HTTPS bootstrap endpoint |
| `RVA_DEVICE_BOOTSTRAP_TOKEN` | bootstrap credential；开发值只放 ignored local input |
| `RVA_WIFI_PRIMARY_SSID` / `RVA_WIFI_FALLBACK_SSID` | provisioning fallback；不得写入 tracked defaults |
| `RVA_WIFI_PRIMARY_PASSWORD` / `RVA_WIFI_FALLBACK_PASSWORD` | Wi-Fi secret；不得进入源码、日志或 artifact metadata |

生成配置头、sdkconfig、binary 可能含部署 secret，均视为敏感 artifact，不提交或公开分发。设备 NVS token 按
WebSocket origin 绑定；endpoint origin 变化时清除旧 token。

`firmware/targets/lichuang-dev/.env.local` 中的 `XIAOZHI_*` 字段只服务显式 compatibility/rollback 构建，不是
native 配置接口，也不得被主线脚本或文档示例消费。

## 4. Runtime 短期材料

- Connect grant：短 TTL，绑定 Worker/device/epoch/fence/profiles/jti；Worker admission 经 Director 在 shared
  coordination store 原子单次消费，Redis-backed 重建/跨实例 replay 测试已覆盖。
- UDP key/salt：每 session、每方向独立；只在 endpoint/Worker memory 中存在，WSS close/expiry 时销毁。
- `session_id/media_id`：不是凭据，但日志仍应限制关联和保留。

Redis 只保存必要的 grant consumption/control metadata，不保存 bearer 原文、UDP key 或媒体。短期材料不得通过
Redis 媒体数据结构、metrics label 或 error response 泄露。

## 5. 本地操作

```powershell
Copy-Item .env.example .env
```

Firmware 开发值通过 `menuconfig` 或 ignored `sdkconfig.local` 注入。填值后运行 repository/secret checks。不得使用
`git add .`；提交前检查 staged diff 和 staged secret scan。Compatibility target 的本地模板只在执行回滚演练时
按其 README 单独创建。

## 6. 轮换

### Grant signing key

目标采用 key id + active/previous 双读窗口：先部署 Worker 验证新旧 key，再切 Director 签新 key，等待旧 grant
TTL 过期，最后移除旧 key。当前单 key实现不支持无中断轮换，属于生产缺口。

### Internal/provider secret

先使 consumer 支持新值，更新 secret manager 和 deployment，验证成功后撤销旧值。Provider key 泄露时同时
检查调用/费用审计，不只替换文件。

### Device credential

撤销应阻止新 bootstrap；既有短期 grant 到期后失效。高风险事件可 drain/close相关 Worker session，但不在
Director 中直接操纵媒体。

## 7. 泄露响应

1. 禁止在更多日志/聊天中重复泄露值。
2. 确定 secret 类型、scope、首次暴露和可能副本。
3. 先撤销/轮换，再清理工作树、历史、artifact、CI log 和缓存。
4. 检查异常访问、provider费用、bootstrap/grant和 Redis audit。
5. 增加检测规则和回归测试；仅删除文件不等于完成处置。
