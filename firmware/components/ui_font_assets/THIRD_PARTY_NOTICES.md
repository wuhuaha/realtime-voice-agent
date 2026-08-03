# 字体资产第三方声明

`ui_font_assets` 的容器、校验、CBIN 相对指针解析和运行期生命周期由本项目维护。默认资产来自以下固定输入：

| 输入 | 上游 | 固定版本 | 许可证及随附文本 |
| --- | --- | --- | --- |
| `font_noto_qwen_20_4.bin` | <https://github.com/78/xiaozhi-fonts> | component `1.6.0`，ZIP SHA-256 `255868d6e225d08038f38add8f7f2bf2e3567ef7a3b0edcd9703d2101f56e7d5`，CBIN SHA-256 `601422de3a49c05265ed853c8054b73b532729e667a6d63f34bb72eab1935345` | Noto-derived 字体数据按 SIL OFL 1.1 随附；组件元数据声明 MIT，但固定包未附 MIT 许可证正文或版权声明，该声明只作为 metadata evidence（见 `third_party/licenses/xiaozhi-fonts-MIT.txt`） |

Noto Sans CJK 源字体版权声明：

> Copyright © 2014-2021 Adobe (http://www.adobe.com/).

下载 URL、目标文件名和 hash 均固定在
`firmware/apps/voice_terminal/tools/build_font_assets.py`。构建缓存、组件归档、提取的 CBIN、
`font_assets.bin` 和 firmware binary 均位于 ignored build tree，不提交到仓库。发布固件时必须随产品第三方声明
提供 SIL Open Font License 1.1 文本。`xiaozhi-fonts` 的 MIT metadata 与缺失许可证正文/版权声明的事实单独记录；
项目不据此生成 MIT 许可证正文或虚构 attribution，SBOM 也不得把 metadata-only 声明标成 verified license。若后续
发布范围包含组件包中除 Noto-derived CBIN 以外的代码或资产，必须另行取得并复核对应许可。精确下载 URL、版本、
ZIP/CBIN hash 与证据路径记录在 `third_party/sources.lock.yaml`。
