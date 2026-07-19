# Firmware dependency locks

`xiaozhi-esp32.dependencies.lock` 是固定 upstream revision 在已验证构建中解析出的完整 ESP-IDF
Component Manager lock。其 SHA256 由仓根 `third_party/sources.lock.yaml` 管理；
`materialize-upstream.ps1` 在复制到 ignored checkout 前后均校验内容，不允许静默重新解析后覆盖。
