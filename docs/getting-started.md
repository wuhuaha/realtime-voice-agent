# Getting started

本指南提供三条相互独立的入口：零凭据协议验证、真实 provider 服务和 ESP32-S3 发布固件。首次使用应先完成零凭据
验证，再引入外部服务或硬件，避免把环境问题误判为协议问题。

## 1. 零凭据 WSS/UDP 验证

前置条件：Linux、macOS 或 WSL，Python 3.12、Git、`uv`。Server 不支持 Windows 原生生产运行。

```bash
git clone https://github.com/wuhuaha/realtime-voice-agent.git
cd realtime-voice-agent
uv sync --directory server --locked --all-packages --dev
uv sync --directory clients/desktop_reference --locked --extra test
uv run --directory clients/desktop_reference pytest \
  tests/e2e/test_deterministic_host.py -m e2e_host -vv
```

测试使用临时端口和测试凭据启动独立 Director/Worker，依次验证`wss-opus/1`和`udp-opus-gcm/1`。成功退出还要求
session、route、子进程和端口完成回收。该路径不访问 ASR、LLM、TTS、Redis、公网或声卡。

## 2. 启动真实服务

从仓库根目录创建 ignored 配置，并替换所有`replace-with-*`值：

```bash
cp .env.example .env
chmod 600 .env
```

至少配置：

- `VOICE_INTERNAL_TOKEN`、`VOICE_GRANT_SIGNING_KEY`和设备 bootstrap credential；
- `VOICE_RUNNER=livekit`；
- FunASR、LLM 和 TTS endpoint、model 与必要 API key；
- 对外 Worker WSS URL；启用 UDP 时还需可达的 UDP advertise host/port。

两个终端都从仓库根目录加载同一配置：

```bash
set -a; source .env; set +a
uv run --directory server session-director
```

```bash
set -a; source .env; set +a
uv run --directory server realtime-worker
```

确认 readiness：

```bash
curl --fail http://127.0.0.1:8080/health/ready
curl --fail http://127.0.0.1:8081/health/ready
```

`ready`只证明配置、coordination和有限 provider 网络探针通过，不等于真实语音闭环。单机 Redis/容器路径见
[`deployment/single-node/README.md`](../deployment/single-node/README.md)。

## 3. Desktop Reference Client

交互模式需要本机 PortAudio 可用的输入和输出设备：

```bash
uv sync --directory clients/desktop_reference --locked --extra interactive
uv run --directory clients/desktop_reference rva-desktop interactive \
  --director-url https://voice.example.com \
  --token-file /secure/rva-bootstrap.token \
  --profile wss-opus/1
```

先验证 WSS，再显式改为`udp-opus-gcm/1`。CLI 不接受明文`--token`，避免凭据进入进程列表或 shell 历史。完整参数见
[Desktop Reference README](../clients/desktop_reference/README.md)。

## 4. ESP32-S3 发布固件

从 [`v0.1.0-alpha.1` Release](https://github.com/wuhuaha/realtime-voice-agent/releases/tag/v0.1.0-alpha.1) 下载：

- `rva-firmware-public-v0.1.0-alpha.1.zip`
- `SHA256SUMS-v0.1.0-alpha.1.txt`

先核对外层 SHA-256，解压后再验证 bundle：

```bash
sha256sum --check SHA256SUMS-v0.1.0-alpha.1.txt
unzip rva-firmware-public-v0.1.0-alpha.1.zip -d rva-firmware
cd rva-firmware
python rva-device-provision.py validate --bundle .
python rva-device-provision.py flash --bundle . --dry-run
```

真实 flash 只写五个公共镜像，不写或擦除 NVS。Wi-Fi、Director bootstrap URL 和每设备 credential 通过
`provision`单独写入；私密配置文件必须位于仓库和 bundle 之外。完整命令、安全边界和 ESP-IDF 5.5.2 前置条件见
bundle 内`FLASHING.md`。

## 5. 下一步

- 架构：[System architecture](architecture/system.md)
- 协议：[Protocol overview](protocol/overview.md)
- 部署：[Deployment](operations/deployment.md)
- 故障排查：[Troubleshooting](operations/troubleshooting.md)
- 已验证范围：[Release readiness](quality/release-readiness.md)
- 未承诺能力：[Known limitations](quality/known-limitations.md)
