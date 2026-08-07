# Desktop Reference Client

`rva-desktop` 是 RVA Protocol 1.0 的 Python 桌面参考端点，用于验证 Product 内 canonical control/media contract、
Director bootstrap、`wss-opus/1` / `udp-opus-gcm/1` transport 与 playback facts。它提供可复现的
headless fixture 路径和使用本机麦克风/扬声器的 interactive 路径。

它不是面向最终用户的桌面产品，不替代 ESP32 firmware、声学/AEC 验证、真实设备 HIL、弱网/长稳验证，
也不是已完成签名、安装器、自动更新、许可证归档或 SBOM 的可发布桌面分发物。

## 安装

要求 Python 3.12 和 `uv`。从 Product 仓根目录执行：

```bash
# headless：包含 Opus codec，不打开本机音频设备
uv sync --directory clients/desktop_reference --locked --extra opus

# interactive：在 Opus codec 之外包含 sounddevice
uv sync --directory clients/desktop_reference --locked --extra interactive

# 开发/验证：包含测试、Ruff 和 Opus codec
uv sync --directory clients/desktop_reference --locked --extra test
```

基础依赖不包含 Opus codec；实际运行 headless 至少需要 `opus` extra，interactive 使用
`interactive` extra。安装成功后通过 console script 查看参数：

```bash
uv run --directory clients/desktop_reference rva-desktop --help
```

## 配置与凭据

| 环境变量 | 用途 | 默认值 |
| --- | --- | --- |
| `RVA_DIRECTOR_URL` | 绝对 Director `https://` URL | 必填，可由 `--director-url` 覆盖 |
| `RVA_BOOTSTRAP_TOKEN` | bootstrap token | 无；更推荐受限权限的 `--token-file` |
| `RVA_DEVICE_ID` | bootstrap device identifier | `desktop-reference` |
| `RVA_TENANT_ID` | tenant identifier | `default` |
| `RVA_MEDIA_PROFILE` | `wss-opus/1` 或 `udp-opus-gcm/1` | `wss-opus/1` |
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

```bash
uv run --directory clients/desktop_reference rva-desktop headless \
  --director-url https://director.example \
  --token-file /secure/rva-bootstrap.token \
  --profile wss-opus/1 \
  --input-pcm ./input.s16le.pcm \
  --output-pcm ./output.s16le.pcm
```

显式 UDP：

```bash
uv run --directory clients/desktop_reference rva-desktop headless \
  --director-url https://director.example \
  --token-file /secure/rva-bootstrap.token \
  --profile udp-opus-gcm/1 \
  --input-pcm ./input.s16le.pcm \
  --output-pcm ./output.s16le.pcm
```

## Interactive

interactive 从本机默认音频输入采集，并播放到默认输出；`--input-device` / `--output-device` 接受
sounddevice 的设备 index 或名称。退出使用 `Ctrl+C`。

扬声器 backend 在阻塞写完成后，以 PortAudio `stream.time + output latency` 形成保守的 host render boundary，
跨过该边界后才发送 playback facts。它仍是驱动报告的预计值，不是实际 DAC、扬声器或声学测量结果；真实设备
若低报 latency，事实仍可能偏早，因此 interactive 必须作为显式 smoke 单独记录设备与 backend。

```bash
uv run --directory clients/desktop_reference rva-desktop interactive \
  --director-url https://director.example \
  --token-file /secure/rva-bootstrap.token \
  --profile wss-opus/1
```

## 验证与证据边界

快速验证不启动 Server 拓扑：

```bash
uv run --directory clients/desktop_reference ruff check src tests
uv run --directory clients/desktop_reference pytest -m "not e2e_host"
```

Linux 上的 deterministic host E2E 会使用独立端口启动 Product 的 Director/Worker 子进程、选择 deterministic
runner，并分别执行 WSS 与 UDP 的 control/media round trip、Opus encode/decode、playback facts 和资源清理：

```bash
uv run --directory clients/desktop_reference pytest -m e2e_host
```

该 E2E 需要已同步的 Server 依赖；Windows 开发者应在 WSL/Linux container 中运行。它形成的只是本机 loopback、
deterministic provider、临时凭据和独立进程拓扑的 host evidence；不证明公网 TLS、真实 provider、目标部署、音频
设备、声学、ESP32、弱网、长稳或 release artifact。临时环境文件、token、端口和原始日志不得成为发布凭据或正式
证据。

Linux root 环境可用 network namespace 和 `tc/netem` 对动态 Worker media 端口运行固定矩阵：

```bash
sudo clients/desktop_reference/tools/run-netem.sh \
  --profiles wss-opus/1,udp-opus-gcm/1 \
  --scenarios clean,delay,random-loss-1,random-loss-3,random-loss-5,burst-loss,jitter,reorder,udp-blocked \
  --repeats 5 --seed 20260805 --output artifacts/netem
```

同一scenario/repeat的WSS与UDP共享逻辑`pair_id`和请求seed，但逻辑配对不等于内核随机序列已配对。harness先探测
当前`tc netem`是否支持`seed`：支持时记录`tc_seed_control=applied`、`paired_randomness=true`；不支持时自动去掉
seed重建qdisc，并记录`tc_seed_control=unavailable`、`paired_randomness=false`和
`comparison_limit=completion_only_unpaired_random_impairment`。后一种结果只能作为isolated loopback netns completion
matrix，不能进行随机loss/jitter/reorder的WSS/UDP配对比较。

TCP和UDP分别绑定`30:`与`40:` netem handle，raw JSONL保存当前profile对应handle的结构化counter及完整
qdisc/filter统计；clean或`udp-blocked`下不适用的WSS不要求impairment counter。受扰profile必须有非零admitted
counter才算规则命中；loss/reorder在有限样本中可能记录`no_impairment_observed`，这只表示规则已穿过但本次未
观察到drop/reorder，不能伪报实际扰动。

raw还记录profile、scenario、pair identity、session completion latency、结果和Worker active/release cleanup；
aggregate明确标记`evidence_scope=completion_only`。正常场景必须媒体闭环且最终`active_sessions=0`，`udp-blocked`
只在明确得到`udp_probe_timeout`且资源归零时符合预期。route cleanup中，第一条lease由随后epoch/fencing前进的
reacquire证明已释放；最终lease只验证release request被接受。该harness尚未采集media age、late/loss/PLC或物理
speech-to-playout，因此不能据此宣称性能SLO、主观质量或WSS/UDP优劣。

`v0.1.0-alpha.1` 的真实 Linux 矩阵确认目标`tc`不支持seed，因此结果明确记录`paired_randomness=false`。修复后的
90-cell矩阵全部满足预期：83次完整`completed`、2次严格`bounded_recovery_verified`和5次预期
`udp_probe_timeout`。该结果只证明受控场景的完成性和有界恢复，不提供物理播放、media-age或transport性能优劣证据。

## 原生依赖与发布边界

`av`（PyAV）会把运行路径带到 FFmpeg/Opus native components，`sounddevice` 会把 interactive 路径带到
PortAudio。不同 OS、architecture、wheel 或系统库组合可能携带不同 native binaries 和 codec build options。
当前 lock 只固定 Python distribution，不等于已完成这些 native components 的来源与分发许可审查。

在任何桌面分发前，必须按实际目标 artifact 完成 PyAV、FFmpeg、libopus、sounddevice、PortAudio 及其传递
native libraries 的许可证确认、许可证文本/notice 收集、source-offer 或 relinking 等适用义务核查，并生成包含
Python 与 native components、版本、来源、hash 和构建选项的 SBOM。该工作尚未完成，因此当前客户端只能作为
source-tree reference/test client，不能标记为 release-ready。
