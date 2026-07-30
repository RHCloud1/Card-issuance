# 部署可靠性改进设计

## 目标

让面向海外 VPS 的首次部署在常见环境差异下更容易成功，并在失败时给出中文、可操作的诊断信息。

本次改动解决两类已实际出现的问题：

- 宿主机或 Docker 向容器传入不可用的 `127.0.0.53` DNS，导致 Caddy 无法访问证书颁发机构。
- `80` 或 `443` 已被 V2bX 等服务占用，直到容器启动阶段才报告端口绑定失败。

## 非目标

- 不修改 Docker 守护进程的全局 DNS，避免影响 V2bX 和其他容器。
- 不自动停止、重启或重配置占用端口的第三方服务。
- 不覆盖现有部署者手工维护的 `docker-compose.override.yml`。
- 不改变应用业务功能、数据库结构或支付接口。

## 配置设计

基础 `docker-compose.yml` 为应用容器添加以下可覆盖默认值：

```yaml
dns:
  - "${CONTAINER_DNS_PRIMARY:-1.1.1.1}"
  - "${CONTAINER_DNS_SECONDARY:-8.8.8.8}"
```

HTTPS 模式生成的 Caddy 服务使用相同配置。`setup.sh` 写入：

```dotenv
CONTAINER_DNS_PRIMARY=1.1.1.1
CONTAINER_DNS_SECONDARY=8.8.8.8
```

普通用户无需回答额外问题。需要自定义时可以编辑 `.env`，然后执行：

```bash
docker compose up -d --force-recreate
```

## 安装流程

`setup.sh` 的 HTTPS 流程调整为：

1. 使用中文展示 HTTPS 与 HTTP 两种部署模式。
2. 用户选择 HTTPS 后，逐端口检查 TCP `80` 和 `443` 是否被占用，并检查本项目 Caddy 对相应端口的实际 Docker 映射。
3. 读取运行中 `card-issuance-caddy` 的 Docker `HostIp`/`HostPort` 绑定，并与 `ss` 返回的每个监听端点逐一核对；只有该端口的全部现有监听端点都能匹配本项目 Caddy 的同地址、同端口绑定时才豁免。
4. 任一监听端点无法匹配、Docker 绑定无法检查或输出无法可靠解析时停止部署，显示 `ss` 监听信息和 Docker 端口映射，但不停止任何进程。
5. 收集站点名称、后台路径、管理员账号、域名等配置。
6. 生成带默认 DNS 的 `.env` 和 Compose 覆盖文件。
7. 构建并启动容器。
8. 在限定时间内通过本机 `--resolve` 请求等待 HTTPS 可用。
9. 成功后显示前台、后台和维护命令；超时则显示 Caddy 最近日志以及 `diagnose.sh` 命令。

如果系统没有 `ss`，脚本显示警告并继续，由 Docker 在启动阶段执行最终端口检查。

重复运行配置脚本时，`card-issuance-caddy` 的存在本身不会整体跳过外部端口占用预检。脚本会对 TCP `80` 和 `443` 分别判断，并保留 IPv4/IPv6 绑定地址：缺少映射、映射到其他宿主机地址或端口、同一端口还存在第三方 IPv4/IPv6 监听，或无法证明监听归属时，仍会终止部署。

## 中文提示

以下脚本的交互提示、成功信息和项目自身错误信息改为中文：

- `scripts/install.sh`
- `scripts/install-docker-ubuntu.sh`
- `scripts/setup.sh`
- `scripts/deploy.sh`
- `scripts/update.sh`
- `scripts/diagnose.sh`

第三方命令本身的输出，例如 `apt`、Docker Build 和 Git，保持原样，方便按原始错误搜索资料。

## 诊断设计

`diagnose.sh` 增加：

- 输出应用与 Caddy 容器的 DNS 配置。
- 在 Caddy 容器内解析 Let’s Encrypt 域名，验证容器 DNS。
- 分别保存容器 DNS、应用直连、正常 TLS 校验和 `-k` 连通性对照的退出状态，并输出中文分类总结。
- 明确区分 Caddy/端口问题、DNS 失败、证书未签发或不受信任以及应用容器异常。
- 所有诊断操作保持只读，不自动修改服务器状态。

## 兼容性

- 现有 `.env` 不含 DNS 变量时，Compose 使用默认值，因此更新后仍可启动。
- 当前服务器手工加入的 Caddy `dns` 配置继续有效。
- `update.sh` 不重新生成或覆盖 `docker-compose.override.yml`。
- HTTP 直连模式同样使用应用容器的默认 DNS，但不创建 Caddy。

## 测试

新增 Shell 脚本测试，使用临时目录和假的 `docker`、`ss`、`curl` 命令，不依赖真实 Docker 或公网：

- HTTPS 配置生成应用与 Caddy DNS 默认值。
- `.env` 写入两个 DNS 配置项。
- `80` 或 `443` 被第三方服务占用时，部署在构建前终止并显示中文提示。
- 本项目 Caddy 已运行且每个 IPv4/IPv6 监听端点均与 Docker 绑定匹配时，不把自身端口视为冲突；同端口存在任一第三方地址监听时仍终止。
- HTTPS 模式缺少 `curl` 时在构建前立即以中文提示失败，并断言就绪探测保留正常证书校验、`--resolve` 和超时边界。
- HTTPS 探测成功时显示部署完成。
- HTTPS 探测超时时显示诊断命令并返回非零状态。
- 使用假的 `docker`、`nslookup`、`curl` 覆盖 DNS、应用、证书、Caddy/端口和成功诊断矩阵。
- 所有 Shell 脚本通过 `bash -n` 语法检查。

## 运维结果

新安装默认不再依赖宿主机提供给容器的本地 DNS stub。端口冲突会在耗时构建前被发现，证书尚未签发时也不会提前宣告部署成功。整个改动只作用于 Card Issuance Compose 项目，不改变 VPS 上其他服务。
