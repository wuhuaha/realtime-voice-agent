# 第三方来源

`sources.lock.yaml` 固定外部源码 revision、许可证与用途。外部源码 materialize 到 ignored `external/`，不以
vendor checkout 形式提交。

`sources.lock.yaml` 只为显式下载的源码和资产保存来源证据，不替代完整依赖清单。Release-only SBOM 从 Server、
Desktop Reference、ESP-IDF component 和本文件的锁定输入确定性生成：

```bash
uv run python scripts/generate_release_sbom.py --output artifacts/release-sbom.cdx.json
uv run python scripts/generate_release_sbom.py --output artifacts/release-sbom.cdx.json --check
```

SBOM 中的组件清单和 hash 来自锁文件。只有随仓库保存了可复核许可证正文的来源才写入 verified license；仅有上游
package metadata 声明时，会保留为 evidence property，不把声明提升为已验证许可证。发布二进制仍须同时携带
`NOTICE`、适用的 `third_party/licenses/` 文本和组件自己的第三方声明。
