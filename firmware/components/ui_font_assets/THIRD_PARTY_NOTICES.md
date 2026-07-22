# 字体资产第三方声明

`ui_font_assets` 的容器、校验和运行期生命周期由本项目维护。默认资产由以下固定输入生成：

| 输入 | 上游 | 固定版本 | 许可证及随附文本 |
| --- | --- | --- | --- |
| Noto Sans CJK SC Regular | <https://github.com/notofonts/noto-cjk> | `Sans2.004` / commit `523d033d6cb47f4a80c58a35753646f5c3608a78`，源文件 SHA-256 `2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b` | SIL Open Font License 1.1 (`third_party/licenses/Noto-Sans-CJK-OFL-1.1.txt`) |
| `lv_font_conv` | <https://github.com/lvgl/lv_font_conv> | `1.5.3` / commit `899ea1128d2e82bb015a319c8a7d18a82359ab3a`，npm tarball SHA-256 `9f64fb8eb553dbab1990402eae74afbafd80b4f39a8314a01484083b6ed1000d` | MIT (`third_party/licenses/lv-font-conv-MIT.txt`) |

Noto Sans CJK 源字体版权声明：

> Copyright © 2014-2021 Adobe (http://www.adobe.com/).

下载 URL、hash、字符范围、字号和 bpp 均固定在
`firmware/apps/voice_terminal/tools/build_font_assets.py`。构建缓存、源 OTF、转换器归档、生成的 CBin、
`font_assets.bin` 和 firmware binary 均位于 ignored build tree，不提交到仓库。发布固件时必须随产品的
第三方声明提供 MIT 与 SIL Open Font License 1.1 文本。精确下载 URL、revision、hash 与许可证路径也记录在
`third_party/sources.lock.yaml`；许可证文件逐字取自对应固定 tag 的官方上游。
