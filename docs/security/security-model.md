# 安全模型

状态：首版安全边界与已知缺口
更新日期：2026-07-20

## 1. 保护目标

- 设备、租户和 session 身份不被冒用。
- 音频、转写、回复、provider credential 和 Wi-Fi credential 保密。
- Control/UDP packet 不被篡改、重放或跨 session 注入。
- 一个 route/session 不出现第二 active owner。
- 未认证输入不能消耗无界 CPU、内存、日志或网络响应。
- 运维可以轮换、撤销、审计和最小化数据保留。

## 2. 对手与假设

考虑公网扫描、伪造 WSS/UDP、被动监听、重放、错误设备、泄露的短期 grant、恶意大包、provider 故障和内部
配置误用。首版不以物理攻破 ESP32、固件 secure boot/flash encryption 或完整企业 PKI 已完成为前提；这些需
独立硬件安全方案。

## 3. 身份链

```text
device bootstrap credential
  -> Director authenticated bootstrap
  -> fenced route lease
  -> signed worker-bound short grant
  -> Worker verification + single consumption
  -> WSS session + optional UDP directional keys
```

Grant claims 必须绑定 `iss/aud/tenant_id/device_id/worker_id/session_epoch/fencing_token/profiles/iat/exp/jti`。
HMAC 比较使用 constant-time API。生产 signing key 至少 128 bit 随机强度并支持版本化轮换。

当前 Worker 在 admission 时调用 Director internal consume API；Director 将 `jti` 与 route/fencing 关联，并在 shared
coordination store 中原子单次消费。Redis-enabled tests 已覆盖重复消费、重建 service/store 后 replay 拒绝及跨
实例行为。该结论解决 process-local replay 缺口，但不等于 Redis HA、网络分区或生产故障演练已完成。

共享 lab token 和 Director bootstrap token 是兼容/开发入口，不是完善的设备 enrollment/rotation 体系；生产需
限制、分设备化或替换。

## 4. WSS

- 生产使用 TLS，证书和 hostname 校验开启。
- Hello/media 前完成 credential、header、size 和 state 校验。
- Per-message compression 关闭。
- 限制 handshake、message、queue、ping、session count 和 principal rate。
- Origin 变化不转发旧 token；ESP32 token 与 endpoint origin 绑定。

Lifecycle repair `d2fa0ca` 已在 Settings 层拒绝缺 host、错误路径及带 query/fragment 的 public endpoint，并要求
production 使用 `wss://`；TLS 证书、
公网 gateway 和公开路径仍需部署验收。

## 5. UDP

- 每 session 上下行独立 AES-128-GCM key/salt，nonce 为 salt + sequence。
- 完整 header 作为 AAD，使用 16-byte tag。
- Authentication 成功后才提交 replay、绑定 source 或解码。
- Source pin、64-packet replay window、sequence exhaustion、expiry 和 MTU hard limit。
- 未认证包静默丢弃，不响应，防止 amplification/log flooding。
- WSS 断开立即废弃 UDP material；不做 NAT rebinding。

UDP key 在 Worker session memory 和设备 session owner 中短期存在，不写 Redis、日志、crash artifact 或 metrics。

## 6. Coordination 与 ownership

- Redis 使用私网/TLS/认证和独立 least-privilege account。
- Lease/fencing 原子操作；store 失败时 Director fail closed。
- Director 不保存媒体或 provider stream。
- Worker 不信任 heartbeat 自报之外的 grant；连接时仍验证 Worker/device/profile/expiry。
- Media hot path 不访问 Redis，降低共享 store 被打爆时的实时影响。

## 7. Provider 与数据

- API key 只由 Worker读取，Director和设备不持有。
- Provider client有 timeout、concurrency、egress allow-list和错误脱敏。
- 默认不记录 raw audio、完整 STT/TTS 文本或 provider body。
- `VOICE_ASR_RECORDING_ENABLED` 等诊断必须默认关闭；开启需获授权、使用隔离目录、设置访问和删除期限。
- 多租户、内容安全、合规地域和 provider 数据使用条款尚需部署方独立评审。

## 8. 威胁与控制

| 威胁 | 当前/目标控制 | 状态 |
| --- | --- | --- |
| Grant 篡改 | HMAC + claims validation | `unit_verified`；公网审计 `not_run` |
| Grant 重放 | shared atomic `jti` single consumption | Redis 重建/跨实例 `unit_verified` |
| 双 Worker owner | Redis lease + fencing | Redis race/fencing `unit_verified`；真实故障演练 `not_run` |
| UDP 注入/重放 | AEAD + replay + source pin | fixture/host 证据，HIL `not_run` |
| UDP amplification | auth-before-response + size/rate | host review，公网 `not_run` |
| WSS 大包/拥塞 | hard size/queue/timeouts | 部分实现，压力 `not_run` |
| Secret 泄露 | ignored env + scan + redaction | repository checks，运维审计 `not_run` |
| 音频隐私 | default no recording + explicit diagnostics | policy defined，部署审计 `not_run` |
| Provider 滥用 | bulkhead/timeout/egress/rotation | 部分实现，故障注入待验证 |

## 9. 不在首版证明范围

ESP32 secure boot、flash encryption、hardware-backed key、设备证书 enrollment、企业 SSO/RBAC、完整多租户隔离、
DDoS 服务、合规认证和内容安全不因本模型而自动完成。纳入产品前必须单独决策和测试。
