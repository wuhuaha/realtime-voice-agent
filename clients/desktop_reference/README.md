# Desktop Reference Client

`rva-desktop` 是 RVA v2 的 Python 桌面参考端点，用于验证 Product 内 canonical control/media contract、
Director bootstrap、`wss-opus-v3` / `udp-opus-gcm-v2` transport 与 playback facts。它提供可复现的
headless fixture 路径和使用本机麦克风/扬声器的 interactive 路径。

它不是面向最终用户的桌面产品，不替代 ESP32 firmware、声学/AEC 验证、真实设备 HIL、弱网/长稳验证，
也不是已完成签名、安装器、自动更新、许可证归档或 SBOM 的可发布桌面分发物。

## 安装

要求 Python 3.12 和 `uv`。从 Product 仓根目录执行：

```powershell
# headless：包含 Opus codec，不打开本机音频设备
uv sync --directory clients/desktop_reference --locked --extra opus

# interactive：在 Opus codec 之外包含 sounddevice
uv sync --directory clients/desktop_reference --locked --extra interactive

# 开发/验证：包含测试、Ruff 和 Opus codec
uv sync --directory clients/desktop_reference --locked --extra test
```

基础依赖不包含 Opus codec；实际运行 headless 至少需要 `opus` extra，interactive 使用
`interactive` extra。安装成功后通过 console script 查看参数：

```powershell
uv run --directory clients/desktop_reference rva-desktop --help
```

## 配置与凭据

| 环境变量 | 用途 | 默认值 |
| --- | --- | --- |
| `RVA_DIRECTOR_URL` | 绝对 Director `https://` URL | 必填，可由 `--director-url` 覆盖 |
| `RVA_BOOTSTRAP_TOKEN` | bootstrap token | 无；更推荐受限权限的 `--token-file` |
| `RVA_DEVICE_ID` | bootstrap device identifier | `desktop-reference` |
| `RVA_TENANT_ID` | tenant identifier | `default` |
| `RVA_MEDIA_PROFILE` | `wss-opus-v3` 或 `udp-opus-gcm-v2` | `wss-opus-v3` |
| `RVA_ALLOW_INSECURE_LOOPBACK` | 允许显式的本机 `http://` 测试 | `false` |

CLI 故意不提供 `--token`：不要把 token 写入 argv、命令历史、日志或文档。优先使用仅当前用户可读、
UTF-8、1..4096 bytes 的 `--token-file`；`RVA_BOOTSTRAP_TOKEN` 只适合已控制环境继承和日志采集的场景。
`--token-file` 存在时优先于环境变量。正常环境使用 `https://` Director；明文 `http://` 只允许 loopback，
并且必须显式传 `--allow-insecure-loopback` 或设置对应环境变量。

每次运行只声明一个 media profile，不存在 `auto` 或静默 fallback。Director/Worker 必须允许所选 profile；
UDP 运行还要求 grant 中的 UDP endpoint 可从桌面主机访问。

## Headless

输入与输出 fixture 是 headerless PCM：16 kHz、mono、signed 16-bit little-endian。输入不足一个 60 ms frame
时会补零；未给出 `--input-pcm` 时发送 `--silence-frames`（默认 10）个静音 frame。headless 在一次 playback
完成后退出，`--timeout`（默认 30 秒）限制整个 run。

显式 WSS：

```powershell
uv run --directory clients/desktop_reference rva-desktop headless `
  --director-url https://director.example `
  --token-file C:\secure\rva-bootstrap.token `
  --profile wss-opus-v3 `
  --input-pcm .\input.s16le.pcm `
  --output-pcm .\output.s16le.pcm
```

显式 UDP：

```powershell
uv run --directory clients/desktop_reference rva-desktop headless `
  --director-url https://director.example `
  --token-file C:\secure\rva-bootstrap.token `
  --profile udp-opus-gcm-v2 `
  --input-pcm .\input.s16le.pcm `
  --output-pcm .\output.s16le.pcm
```

## Interactive

interactive 从本机默认音频输入采集，并播放到默认输出；`--input-device` / `--output-device` 接受
sounddevice 的设备 index 或名称。退出使用 `Ctrl+C`。

扬声器 backend 在阻塞写完成后，以 PortAudio `stream.time + output latency` 形成保守的 host render boundary，
跨过该边界后才发送 playback facts。它仍是驱动报告的预计值，不是实际 DAC、扬声器或声学测量结果；真实设备
若低报 latency，事实仍可能偏早，因此 interactive 必须作为显式 smoke 单独记录设备与 backend。

```powershell
uv run --directory clients/desktop_reference rva-desktop interactive `
  --director-url https://director.example `
  --token-file C:\secure\rva-bootstrap.token `
  --profile wss-opus-v3
```

## 验证与证据边界

快速验证不启动 Server 拓扑：

```powershell
uv run --directory clients/desktop_reference ruff check src tests
uv run --directory clients/desktop_reference pytest -m "not e2e_host"
```

Windows 上的 deterministic host E2E 会使用独立端口启动 Product 的 local Director/Worker、选择
deterministic runner，并分别执行 WSS 与 UDP 的 control/media round trip、Opus encode/decode、playback facts
和资源清理：

```powershell
uv run --directory clients/desktop_reference pytest -m e2e_host
```

该 E2E 需要 `pwsh` 和已同步的 Server 依赖。它形成的只是本机 loopback、deterministic provider、临时凭据和
独立进程拓扑的 host evidence；不证明公网 TLS、真实 provider、目标部署、音频设备、声学、ESP32、弱网、长稳
或 release artifact。临时环境文件、token、端口和原始日志不得成为发布凭据或正式证据。

## 原生依赖与发布边界

`av`（PyAV）会把运行路径带到 FFmpeg/Opus native components，`sounddevice` 会把 interactive 路径带到
PortAudio。不同 OS、architecture、wheel 或系统库组合可能携带不同 native binaries 和 codec build options。
当前 lock 只固定 Python distribution，不等于已完成这些 native components 的来源与分发许可审查。

在任何桌面分发前，必须按实际目标 artifact 完成 PyAV、FFmpeg、libopus、sounddevice、PortAudio 及其传递
native libraries 的许可证确认、许可证文本/notice 收集、source-offer 或 relinking 等适用义务核查，并生成包含
Python 与 native components、版本、来源、hash 和构建选项的 SBOM。该工作尚未完成，因此当前客户端只能作为
source-tree reference/test client，不能标记为 release-ready。
