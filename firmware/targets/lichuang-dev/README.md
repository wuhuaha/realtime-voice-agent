# Lichuang Dev production firmware target

本目录是立创实战派 ESP32-S3 的唯一 production firmware composition，保存 pinned Xiaozhi upstream 所需的
项目自有 overlay 和复现脚本；上游源码 materialize 到 ignored 的 `external/xiaozhi-esp32/`，不得整仓提交。

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

首轮保留 Xiaozhi 原版 UI、audio/AEC、Opus task/queue 和 WebSocket owner。overlay 当前只做五类变更：

1. local-lab 模式跳过 Xiaozhi 云 OTA bootstrap；
2. 强制使用上游 `WebsocketProtocol` 连接 `/v1/xiaozhi`；
3. NVS 尚无 Wi-Fi 时安装 ignored 配置中的默认网络。
4. 每次新建音频连接时解析一次不可变 endpoint 快照，允许板上 NVS 覆盖 local-lab 默认值。
5. 将 assistant 文案收口为“AI”，并用有界音频发送批次避免 realtime 上行饿死 UI toggle/wake 控制事件。

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

历史 `0001` 至 `0010` WSS/UDP 序列已在新仓 ignored checkout 独立完成 canonical contract、
patch round-trip 和 ESP-IDF 5.5.2 clean build：`2215/2215`，app `0x2d2660` bytes，最小 app
partition 余量 28%，DIRAM `170,887 / 341,760` bytes。包含本机 local config 的 ignored artifact
SHA256 为 `43bac4d4ed678b3298cc9f4c8e9da0c4ab7608af731406cec31939ee457350c8`；该 hash 只标识
本次 artifact，不代表 bit-reproducible，也不得公开 artifact 中的 local credentials。

该历史 reference 序列已在交付仓提交 `cf9bc69` 下烧录到 `COM11` 的 ESP32-S3 revision 0.2（8 MiB PSRAM）：先擦除
`0x9000` 起始的 16 KiB NVS，再按 flasher manifest 写入 bootloader、partition table、otadata、app 与
assets；五个区域 read-back SHA256 全部匹配 [artifacts.sha256](../../../migration/baseline/artifacts.sha256)。
cold boot 后连接 Wi-Fi“广告位招租”并获得 `192.168.1.105`；display、audio codec、ES7210、AEC、VAD、
wake model 初始化完成，观察窗口内无 panic/WDT。唤醒成功，WSS 握手约 20 ms；UDP GCM 首个
authenticated probe 成功并进入 ready，随后观察到 600+ UDP Opus uplink packets，AEC 为 `VOIP_HIGH_PERF`。

上述 HIL 只证明该历史 artifact 的烧录、bring-up、唤醒和上行 transport。历史 `0011..0014` 与 managed
`0003..0008` composition 使用固定公网 Director 配置完成 ESP-IDF 5.5.2 clean build，app SHA-256 为
`61542dad78a11a130263952e4148f9b7c70b1e8919e3f2ca192d21612e6716a3`，并完成 COM11 app-only 烧录与 hash
verified。app 为 `2,970,272` bytes、partition 余量 28%，DIRAM 为 `170,991 / 341,760`（50.03%）。该精确
artifact 已由电脑 TTS 唤醒，完成公网 Director/WSS、AFE AEC、ASR“请用一句话介绍你自己”、流式字幕、
`listening -> speaking -> listening` 与板端 playback；100 帧 underrun 0、max write 62.3 ms，无 ERROR/panic/WDT。

`0014` 前 `cb544...` artifact 曾完成公网 TTS/playout 和 350 帧零 underrun。superseded `9026...` artifact 已完成
公网 Director/WSS、AFE AEC、真机 ASR 与流式字幕，无 panic/WDT；Server 观察到 FunASR、DeepSeek、remote
CosyVoice 和 `session_closed reason=user_initiated`，但无法确认关闭来源是物理触屏。这些证据不继承给当前
`61542...`。该 artifact 暴露了本地主动关闭未通知 Application 的 lifecycle 缺口，不再代表当前点击关闭行为。

当前 `0011..0015` composition 的 clean build 完成 `2215/2215`，app 为 `2,970,512` bytes，SHA-256 为
`394fc4b380a3269aef424b5836d46d806457fa40c38249d3d8c57e3c45562aed`，partition 余量 28%，DIRAM 为
`170,991 / 341,760`（50.03%）；已 app-only 烧录到 COM11 并 hash verified。物理点击聆听中的麦克风后，串口
记录 `Audio channel closed generation=8`，随后状态机从 `listening` 进入 `idle`。对应忽略日志为
`.runtime/logs/firmware-394fc4b3-toggle-hil.log`，该交互达到 `device_verified`。服务端仍有中断清理 warning，
因此不能声明整个 teardown 无 warning。

当前 artifact 的屏上 Wi-Fi/endpoint 保存重启回读、UDP provider 闭环、完整 UI 视觉验收、真人近讲
20 轮、不同距离/
double-talk 声学测试、弱网和 30 分钟长稳仍为 `not_run`。

`0014-ui-control-responsiveness.patch` 把 `assistant_role` 文案改为“AI”，并把 `MAIN_EVENT_SEND_AUDIO` 从无界
drain 改成每轮最多 4 包；toggle 先执行，停止时同轮跳过发送并清空/隔离旧 generation 上行。
`test-ui-control-contract.ps1` 在 `idf build` 前验证具体函数块、文案、事件顺序、有界批处理、旧上行 fence 与
禁止 unbounded `while`。当前
clean build、boot 和 WSS 链路均包含该 patch；物理屏“AI”视觉仍为 `not_run`。

`0015-local-close-notification.patch` 修复本地主动关闭先使 connection generation 失效、继而让远端 disconnect
callback 被旧 generation fence 拒绝的问题。本地关闭在 WebSocket owner disconnect/release 后、锁外显式通知
Application；本地与远端关闭共用 generation 去重入口，receive task 内关闭仍调度到主任务。focused contract、
clean build 与物理点击 HIL 已通过。
