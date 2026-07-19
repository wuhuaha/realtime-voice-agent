# Xiaozhi WebSocket Opus 端侧 overlay

本目录保存 `xiaozhi-websocket-opus-v1` 的项目自有 overlay 和复现脚本；上游源码 materialize 到 ignored 的 `external/xiaozhi-esp32/`，不得整仓提交。

## 固定基线

- 上游：`https://github.com/78/xiaozhi-esp32`
- revision：`7b190b78e4f8dfef14126f6cd478c134b3cd3cd8`
- 上游应用许可证：MIT；managed components 各自遵循其许可证
- target/board：`esp32s3` / `lichuang-dev`
- 上游 manifest 要求：ESP-IDF `>=5.5.2`
- `esp-sr`：manifest 约束 `~2.3.0`，实际解析版本以构建生成的 lock 为准
- resolved dependency lock：`firmware/locks/xiaozhi-esp32.dependencies.lock`，由 materialize 脚本校验后复制到 ignored checkout

该 revision 的立创板配置明确启用 `AUDIO_INPUT_REFERENCE=true` 和 `CONFIG_USE_DEVICE_AEC=y`。其 AFE 输入格式为 `MR`，device AEC 开启后默认 listening mode 为 `realtime`，播放期间不停止 voice processing。WebSocket v1 上行是 16 kHz、mono、60 ms Opus；连接头包含物理 Wi-Fi MAC `Device-Id`、板 UUID `Client-Id`、`Protocol-Version: 1` 和 Bearer token。

这些是源码/配置事实，不等于本板 AEC、双讲或长稳已经通过。

## Overlay 边界

首轮保留 Xiaozhi 原版 UI、audio/AEC、Opus task/queue 和 WebSocket owner。overlay 只做四件事：

1. local-lab 模式跳过 Xiaozhi 云 OTA bootstrap；
2. 强制使用上游 `WebsocketProtocol` 连接 `/v1/xiaozhi`；
3. NVS 尚无 Wi-Fi 时安装 ignored 配置中的默认网络。
4. 每次新建音频连接时解析一次不可变 endpoint 快照，允许板上 NVS 覆盖 local-lab 默认值。

endpoint 优先级固定为：`voice_agent` NVS > ignored local-lab 配置 > 上游 `websocket` NVS。`voice_agent` namespace 使用 `ws_url`、可选 `token`、`token_origin` 和可选 `protocol_ver`；`protocol_ver` 是逻辑字段 protocol_version 的物理 NVS key（ESP-IDF NVS key 最长 15 字符），省略时默认 `1`，当前也仅支持 `1`。UI 保存 `ws_url` 和 `protocol_ver=1`。凭据按 WebSocket origin（scheme、host、port）绑定：只有目标 origin 与 local-lab、upstream 或 `voice_agent.token_origin` 一致时才会附带对应 token；UI 切换 origin 时清除旧 token，禁止把 bearer 转发到新服务。URL 只接受不超过 255 字节、host 非空的 `ws://` 或 `wss://`。连接日志只记录来源和 host，不打印 token 或完整 URL。已有 Wi-Fi NVS 仍优先。

`78/esp-wifi-connect` 是 component manager 解析的 managed component。`build.ps1` 在 `idf.py set-target` 后应用 `overlay-managed/`：Wi-Fi scan records 只由 `WifiStation` owner 消费，UI 通过 one-shot callback 获取副本，避免多个 `WIFI_EVENT_SCAN_DONE` consumer 争抢结果。

## 配置与构建

首次准备或校验固定 upstream：

```powershell
./scripts/materialize-upstream.ps1 -VerifyInputsOnly
./scripts/materialize-upstream.ps1
./scripts/materialize-upstream.ps1 -VerifyOnly
```

脚本只从仓根 `third_party/sources.lock.yaml` 读取 URL、revision 和 dependency lock SHA256，
不会读取研究仓路径。materialize/verify 只接受无 tracked 改动的 pinned checkout；ignored
`dependencies.lock` 可以存在，脚本不会 reset 或自动覆盖 tracked 工作树。overlay 应在 materialize
完成后单向应用，不能再用 materializer 验证已打 patch 的工作树。

在本目录从模板创建 ignored 的 `.env.local`，不要把 token 或 Wi-Fi 密码写入 tracked 文件：

```powershell
Copy-Item .env.local.example .env.local
```

激活 `third_party/sources.lock.yaml` 固定的 ESP-IDF 后执行：

```powershell
./scripts/build.ps1 -Clean
```

脚本会核对 revision、幂等应用 patch、生成不打印取值的外部配置头、使用独立 `sdkconfig.voice-agent`/`build-voice-agent` 构建并运行 `idf.py size`，最后确认 token 和 Wi-Fi 密码没有进入 Git tracked 文件。
构建前还会按 source lock 拒绝 ESP-IDF version tag、Git revision 或 tracked-clean 状态不一致的工具链；
untracked/ignored 工具缓存不参与该 dirty 判定。

若只准备普通 overlay：

```powershell
./scripts/verify-source-contract.ps1
./scripts/apply-overlay.ps1
./scripts/assert-generated-config.ps1
./scripts/test-endpoint-resolver.ps1
```

`overlay-managed/` 依赖 ESP-IDF Component Manager 已解析出
`managed_components/78__esp-wifi-connect`。fresh checkout 不得直接运行
`apply-managed-overlay.ps1`；应优先使用 `build.ps1`。确需手工准备时，先在上游
checkout 中以同一 build/sdkconfig 目录运行 `idf.py set-target esp32s3` 或
`idf.py reconfigure`，确认 managed component 已生成，再运行该脚本。

## 证据边界

并发与底层网络取消语义的未关闭项见 [KNOWN_DEBT.md](KNOWN_DEBT.md)。

固定 revision 的 `0001` 至 `0005` WSS 基线已完成 clean build、app-only 烧录、cold boot、Wi-Fi、真实 provider 闭环、约 10 分钟 listening smoke、长回复越界回归和一次自动 double-talk/打断。TTS callback carry-buffer 修复与当前环境 VAD `0.60` 配置也已通过真机闭环。

当前 `0001` 至 `0010` WSS/UDP 最终序列已在新仓 ignored checkout 独立完成 canonical contract、
patch round-trip 和 ESP-IDF 5.5.2 clean build：`2215/2215`，app `0x2d2660` bytes，最小 app
partition 余量 28%，DIRAM `170,887 / 341,760` bytes。包含本机 local config 的 ignored artifact
SHA256 为 `43bac4d4ed678b3298cc9f4c8e9da0c4ab7608af731406cec31939ee457350c8`；该 hash 只标识
本次 artifact，不代表 bit-reproducible，也不得公开 artifact 中的 local credentials。

最终序列已在交付仓提交 `cf9bc69` 下烧录到 `COM11` 的 ESP32-S3 revision 0.2（8 MiB PSRAM）：先擦除
`0x9000` 起始的 16 KiB NVS，再按 flasher manifest 写入 bootloader、partition table、otadata、app 与
assets；五个区域 read-back SHA256 全部匹配 [artifacts.sha256](../../../migration/baseline/artifacts.sha256)。
cold boot 后连接 Wi-Fi“广告位招租”并获得 `192.168.1.105`；display、audio codec、ES7210、AEC、VAD、
wake model 初始化完成，观察窗口内无 panic/WDT。唤醒成功，WSS 握手约 20 ms；UDP GCM 首个
authenticated probe 成功并进入 ready，随后观察到 600+ UDP Opus uplink packets，AEC 为 `VOIP_HIGH_PERF`。

上述 HIL 只证明烧录、bring-up、唤醒和上行 transport。未验证 UI 视觉/触摸；自动声学语句采集峰值过低且
未触发 ASR，所以不得声称真机 FunASR/DeepSeek/CosyVoice/downlink playout 闭环。Server `259aeee` 的
host synthetic real-media E2E 独立证明 FunASR final 约 480 ms、DeepSeek HTTP 200/TTFT 约 9,876 ms、
CosyVoice HTTP 200/TTFB 约 594 ms和下行音频，但它不是设备声学闭环。屏上 Wi-Fi/endpoint 保存后的重启
回读、真人近讲 20 轮、不同距离/double-talk声学测试、弱网和 30 分钟长稳仍为 `not_run`。
