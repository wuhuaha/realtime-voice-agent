# Lichuang Dev compatibility target

本目录保存 pinned Xiaozhi upstream、Product overlay 和复现脚本，仅作为 native endpoint 迁移期的
compatibility/rollback lane。新功能进入 `firmware/apps/voice_terminal` 与 `firmware/components`。

## 固定来源

- Upstream：`https://github.com/78/xiaozhi-esp32`
- Revision：`7b190b78e4f8dfef14126f6cd478c134b3cd3cd8`
- Target/board：`esp32s3` / `lichuang-dev`
- ESP-IDF：`>=5.5.2`，实际构建使用仓库锁定 revision
- Dependency lock：`firmware/locks/xiaozhi-esp32.dependencies.lock`
- License：upstream MIT；managed components 遵循各自许可证

上游源码只 materialize 到 ignored checkout，不得整仓提交。Canonical source identity 和 hash 由
`third_party/sources.lock.yaml` 与 `migration/baseline/source-manifest.yaml` 管理。

## 配置与构建

```powershell
./scripts/materialize-upstream.ps1 -VerifyInputsOnly
./scripts/materialize-upstream.ps1
Copy-Item .env.local.example .env.local
./scripts/build.ps1 -Clean
```

`.env.local` 只用于本地回滚验证，不得提交。脚本校验 upstream revision、dependency lock、ESP-IDF identity、
overlay round-trip、生成配置和 secret boundary；构建产物保持 ignored。

Focused source contract：

```powershell
./scripts/verify-source-contract.ps1
./scripts/apply-overlay.ps1
./scripts/assert-generated-config.ps1
./scripts/test-endpoint-resolver.ps1
```

不要手工修改 materialized checkout 后把它当成可复现基线。`overlay-managed` 依赖 Component Manager 已解析的
managed components，fresh checkout 优先使用 `build.ps1`。

## 已知边界

- 该 target 仍使用 Xiaozhi control/application lifecycle，不是 native RVA endpoint。
- 它保留已验证的 board/audio/AEC/WSS/UDP 行为作为 rollback oracle，但历史结果不证明当前 native artifact。
- 并发和第三方边界见 [KNOWN_DEBT.md](KNOWN_DEBT.md)。
- 当前 release gate 见 [Release readiness](../../../docs/quality/release-readiness.md)。

Native endpoint 完成 clean build、WSS/UDP/UI/config/AEC HIL、20 轮和 30 分钟门禁，并结束旧客户端支持期后，
该 target 可连同专用 lock/materialization tooling 一次性删除。Git 历史承担归档职责。
