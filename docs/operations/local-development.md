# 本地开发

更新日期：2026-07-20

## 1. 前置条件

- Windows PowerShell 7。
- Git、Python 3.12 和 `uv`。
- 需要固件时安装 ESP-IDF 5.5.2，并使 `idf.py` 在激活环境可用。
- 需要 production-like coordination 时启动 Redis-compatible service。
- 真实 provider 凭据只写入 ignored `.env`，设备 Wi-Fi/token 只写入 ignored `.env.local`。

## 2. 初始化

```powershell
Copy-Item .env.example .env
./scripts/bootstrap.ps1 -SkipFirmware
```

编辑 `.env`，至少替换所有 `replace-with-*`。默认 `VOICE_COORDINATION_BACKEND=memory` 只适合本地单进程验证。

需要 production firmware source：

```powershell
./firmware/targets/lichuang-dev/scripts/materialize-upstream.ps1
Copy-Item ./firmware/targets/lichuang-dev/.env.local.example `
  ./firmware/targets/lichuang-dev/.env.local
```

`bootstrap.ps1` 的非 `-SkipFirmware` 路径调用仓内 materialize wrapper，并校验固定 upstream 与 ESP-IDF
revision；不要创建未跟踪的手工 checkout 冒充可复现流程。

## 3. 验证

```powershell
./scripts/verify.ps1
```

该入口应运行 root/server Ruff、pytest、repository、secret 和 firmware source contract。固件构建是显式动作：

```powershell
./scripts/verify.ps1 -BuildFirmware
```

构建不烧录。报告必须区分 `build_passed` 与 `device_verified`。目标 component skeleton 可单独检查：

```powershell
./firmware/device/scripts/verify-component-boundaries.ps1
./firmware/device/scripts/build-headless.ps1 -Clean
```

Headless build 只证明 component compile/link，不是带屏、音频或网络固件。

## 4. 启动本地服务

```powershell
./scripts/run-local.ps1 -WorkerCount 2
```

入口要求 `.env`，启动一个 Director 和指定数量 Worker；端口优先读取 ignored `.env`，也可由脚本参数显式
覆盖。多 Worker 会从基准 Worker/UDP 端口递增，生成的进程身份记录位于 `.runtime/local/`。

当前已验证的单 Worker local topology 为 Director `8079`、Worker `8080`、UDP `8092`。检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8079/health/live
Invoke-RestMethod http://127.0.0.1:8079/health/ready
Invoke-RestMethod http://127.0.0.1:8080/health/live
Invoke-RestMethod http://127.0.0.1:8080/health/ready
```

`ready` 中 `provider_network_checked=false` 表示 readiness 未探测真实 provider；配置 Director 时
`coordination_ready` 还要求 heartbeat 至少成功一次。HTTP 200 仍不能单独证明真实语音闭环。

## 5. 开发模式

### Deterministic

设置 `VOICE_RUNNER=deterministic`，用于协议、队列、lifecycle 和多 Worker 测试，不访问真实 provider。

### Real provider

设置 `VOICE_RUNNER=livekit` 并配置 FunASR、LLM 和所选 TTS。首次只运行单 Worker/WSS，确认真实闭环后再启用
UDP 或多 Worker，避免同时改变多个变量。API key 不得出现在命令行、截图、日志或 fixture。

### Coordination

- `memory`：单进程/测试，不能验证水平扩展。
- `redis`：设置 `VOICE_COORDINATION_BACKEND=redis` 与 `VOICE_REDIS_URL`，用于 lease/fencing 多实例验证。

## 6. Firmware 配置与构建

`.env.local` 字段包括 `XIAOZHI_DIRECTOR_URL`、`XIAOZHI_DEVICE_BOOTSTRAP_TOKEN`、`XIAOZHI_WS_URL`、
`XIAOZHI_LAB_TOKEN`、`XIAOZHI_TRANSPORT_MODE`、主/回退 Wi-Fi SSID 与密码。Director bootstrap/短期 grant
是当前主路径；`XIAOZHI_WS_URL` 与 lab token 只用于显式开发回退。配置输入变化后必须使用 `-Clean`，脚本的
config-input guard 会拒绝复用陈旧生成配置。

```powershell
./firmware/targets/lichuang-dev/scripts/build.ps1 -Clean
```

只有用户明确授权目标板和端口后才 flash。每次记录 artifact SHA-256、source revision、overlay digest、IDF、
COM、命令和观察时长。本轮已在 COM11 的 ESP32-S3 rev0.2（8 MB PSRAM）验证：仅擦 NVS
`0x9000/16 KiB`，完整写入 bootloader/partition/otadata/app/assets，全部 hash verified。

## 7. 最小开发顺序

1. `verify.ps1`。
2. deterministic Director/Worker host test。
3. WSS reference client。
4. 单 Worker真实 provider smoke。
5. Reference firmware build/size。
6. 授权后 WSS HIL。
7. 显式 UDP HIL。
8. Redis multi-worker、弱网、声学和长稳。

当前 Server lifecycle repair `d2fa0ca` 已通过精确 tick manifest 的 launcher stop/start。Host synthetic Chinese 经真实 Director grant、
WSS + UDP Opus/GCM 完成 provider media E2E：FunASR final（约 480 ms）、DeepSeek HTTP 200（单次 TTFT 约
9876 ms）、remote CosyVoice HTTP 200（单次 TTFB 约 594 ms）并产生 downlink audio。

当前公网 Director 配置的 production firmware clean build 为 `2,970,272` bytes，SHA-256
`61542dad78a11a130263952e4148f9b7c70b1e8919e3f2ca192d21612e6716a3`。COM11 app-only 烧录 hash verified。
电脑 TTS 唤醒后，该 artifact 完成 public Director/WSS、AFE AEC、ASR、流式字幕、TTS/playout 与状态往返；
100 帧 underrun 0、max write 62.3 ms，无 ERROR/panic/WDT。`0014` source contract 验证“AI”文案、4 包批次、
graceful stop/取消策略和 generation fence，但物理视觉与点击结束未 HIL。当前固件 UDP provider、UI/触摸、
弱网、正式声学、20 轮和
30 分钟仍为 `not_run`。
