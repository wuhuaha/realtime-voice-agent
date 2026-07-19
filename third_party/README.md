# 第三方来源

`sources.lock.yaml` 固定外部源码 revision、许可证与用途。外部源码 materialize 到 ignored `external/`，不以
vendor checkout 形式提交。`firmware/locks/` 可保存为可复现构建所需的解析依赖 lock，但不得包含本机路径、
凭据或生成配置。
