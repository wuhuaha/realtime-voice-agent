# Linux single-node deployment

本目录提供一个 Redis、一个 Session Director 和一个 Realtime Worker 的 Docker Compose 基线，适合开发集成和小容量
单机部署。它不提供 TLS、入口限流、WAF、Redis HA 或多主机故障转移。

## 启动

要求 Linux、Docker Engine 和 Docker Compose 2.24+：

```bash
cd deployment/single-node
cp env.example .env
chmod 600 .env
# 替换所有 replace-with-*、公网 WSS/UDP 地址和 provider 配置
docker compose config --quiet
docker compose build --pull
docker compose up -d
docker compose ps
```

检查服务：

```bash
curl --fail http://127.0.0.1:8080/health/ready
curl --fail http://127.0.0.1:8081/health/ready
docker compose logs --tail=100 director worker
```

公网使用必须在 Compose 外提供受信 HTTPS/WSS gateway。UDP 不能经 HTTP gateway 转发，防火墙/NAT 的外部端口必须
与`VOICE_UDP_PUBLIC_PORT`和设备收到的 advertise port 一致。

## 安全与升级

- `.env`包含设备凭据和 provider secret，不得提交、上传或粘贴完整展开的`docker compose config`。
- 受控发布应使用经过验证的`image@sha256:<digest>`，不要把可变 tag 当作不可变发布身份。
- 普通停止使用`docker compose down`；除非明确接受 coordination 数据丢失，不要使用`down -v`。
- 升级 Worker 前先 drain 并等待 active session 归零；单 Worker 升级会中断已有媒体会话。

完整拓扑、扩容、回滚和安全门禁见 [Deployment guide](../../docs/operations/deployment.md)。
