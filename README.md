# Card Issuance

自建自动发卡系统。当前版本提供前台匿名购买、后台商品/分类/卡密/支付通道管理、订单查询、工单提交和 Docker Compose 部署。

## 功能范围

- 前台无需登录购买
- 前台亮暗模式切换，默认跟随系统
- 商品分类、价格、库存展示
- 联系邮箱下单和订单查询
- 订单号选填查询；不填订单号时按联系邮箱查询全部订单
- 可选取卡密码，用于保护已发放卡密展示
- 前台工单提交
- 后台管理员登录
- 后台分类新增、编辑、删除
- 后台商品新增、改价、排序、上架、下架
- 后台卡密批量导入、去重、作废
- 后台订单、工单查看
- 后台支付通道启停和 JSON 配置
- 支付成功后自动发卡
- 支付宝、微信、聚合支付通道预留

## 后台可配置项

以下内容可以直接在网页后台完成，不需要改代码：

- 分类：新增、编辑、启停、删除
- 商品：新增、编辑、价格、排序、状态、单次限购
- 卡密：批量导入、作废
- 支付通道：名称、费率、启停、JSON 配置
- 订单和工单：查看

需要说明：支付宝、微信或聚合支付的商户号、密钥、证书路径、回调地址等信息可以在后台支付通道 JSON 里保存；但如果要真正接入某个支付网关，仍需要为该网关实现“创建支付请求、验签、回调处理”的适配器代码。配置不等于网关协议已经实现。

## 技术栈

- Python 3.12
- Flask
- SQLite
- Gunicorn
- Docker Compose

SQLite 数据默认挂载在宿主机 `./data` 目录，容器重建不会删除业务数据。

## 目录说明

```text
app/                  Flask 应用代码、模板和静态文件
data/                 SQLite 数据目录，本地运行后生成，不提交 Git
docs/                 架构说明
scripts/install.sh    全新 VPS 一键安装入口
scripts/setup.sh      交互式部署配置脚本
scripts/deploy.sh     启动或重建脚本
scripts/update.sh     后续一键更新脚本
scripts/diagnose.sh   部署诊断脚本
scripts/dev.ps1       Windows 本地开发启动脚本
schema.sql            数据库结构
docker-compose.yml    Docker Compose 配置
Dockerfile            容器镜像构建文件
```

## VPS 环境要求

推荐系统：

- Ubuntu 22.04 LTS 或 Ubuntu 24.04 LTS
- 1 核 1G 起步，建议 1 核 2G
- 已开放 80/443 端口；如果直接访问容器端口，还需要开放 8080

必需软件：

- Git
- Docker Engine
- Docker Compose Plugin

建议在 VPS 上使用 `root` 登录，或先执行 `sudo -i` 后再运行安装脚本。脚本兼容 root 和 sudo 用户，但 Docker 权限最少会省掉一类常见问题。

## 推荐部署方式

如果是全新 Ubuntu VPS，推荐直接运行一键安装入口：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/RHCloud1/Card-issuance/main/scripts/install.sh)
```

如果已经克隆过仓库，可以进入项目目录后重新跑交互配置：

```bash
git pull --ff-only
chmod +x scripts/*.sh
./scripts/setup.sh
```

脚本会依次处理：

1. 安装 Git、Docker 和 Docker Compose Plugin
2. 克隆项目到你指定的目录，默认 `/opt/card-issuance`
3. 询问部署模式
4. 询问域名、后台路径、管理员邮箱、管理员密码等配置
5. 生成 `.env`
6. 生成 `docker-compose.override.yml`
7. 如果选择 HTTPS，自动生成 Caddy 配置并申请证书
8. 构建并启动容器
9. 输出真实前台和后台访问地址

## 443 和 8080 的区别

`8080` 是应用容器内部 HTTP 服务端口。直接访问时地址类似：

```text
http://你的域名或IP:8080/
```

如果浏览器访问 `https://域名:8080`，通常会失败，因为 8080 上跑的是 HTTP，不是 HTTPS。

如果想使用常规的：

```text
https://你的域名/
```

就需要在应用前面放一个能处理 HTTPS 证书的入口。本项目交互脚本默认推荐 **Caddy**，它会监听 80/443，自动申请 Let's Encrypt 证书，并反向代理到应用容器。

也就是说：

- 临时测试：可以选 HTTP 直连端口，例如 8080
- 正式使用：建议选 HTTPS + Caddy，直接走 443

无论哪种方式，都需要确认 VPS 防火墙和云厂商安全组放行对应端口。

## 部署后打不开的排查

先确认访问地址和部署模式匹配：

- HTTPS + Caddy：访问 `https://你的域名/`，不要加 `:8080`
- HTTP 直连端口：访问 `http://你的域名或IP:端口/`
- `https://域名:8080` 基本一定不对，因为 8080 默认不是 HTTPS

然后在项目目录执行诊断：

```bash
cd /path/to/card-issuance
chmod +x scripts/diagnose.sh
./scripts/diagnose.sh
```

常见原因：

- DNS 还没有指向当前 VPS
- 云厂商安全组或系统防火墙没有放行 80/443 或直连端口
- 域名开启了代理/CDN，但源站端口没有放行
- Caddy 申请证书失败，可以看 `docker compose logs -f caddy`
- 应用容器没启动，可以看 `docker compose ps` 和 `docker compose logs -f card-issuance`

## 手动安装 Docker

Ubuntu/Debian 可以使用项目内脚本：

```bash
curl -fsSL https://raw.githubusercontent.com/RHCloud1/Card-issuance/main/scripts/install-docker-ubuntu.sh -o install-docker-ubuntu.sh
bash install-docker-ubuntu.sh
```

也可以在克隆项目后执行：

```bash
chmod +x scripts/install-docker-ubuntu.sh
./scripts/install-docker-ubuntu.sh
```

安装完成后检查：

```bash
docker --version
docker compose version
```

## 手动首次部署

克隆仓库：

```bash
git clone https://github.com/RHCloud1/Card-issuance.git card-issuance
cd card-issuance
```

运行交互式配置：

```bash
chmod +x scripts/*.sh
./scripts/setup.sh
```

脚本会询问以下信息：

- 部署模式：HTTPS + Caddy，或 HTTP 直连端口
- 域名：HTTPS 模式必填，且 DNS 必须指向当前服务器
- TLS 证书邮箱：用于 Let's Encrypt
- 站点名称
- 后台路径
- 管理员登录邮箱
- 管理员密码
- 订单过期分钟数

访问：

- 前台：脚本结束时输出的 `Frontend`
- 后台：脚本结束时输出的 `Admin`

## 宝塔、Nginx 或 Caddy

如果你不用本项目内置 Caddy，也可以让 Docker 只提供 HTTP 端口，由宝塔或 Nginx 负责域名和 HTTPS。

如果反代服务和应用在同一个 Docker 网络里，反向代理目标可以是：

```text
http://card-issuance:8080
```

如果宝塔或 Nginx 在宿主机上运行，并选择了 HTTP 直连端口，则目标通常是：

```text
http://127.0.0.1:8080
```

Nginx 宿主机示例：

```nginx
server {
    listen 80;
    server_name example.com;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

生产环境建议启用 HTTPS。

## 后续一键更新

进入项目目录：

```bash
cd /path/to/card-issuance
```

执行：

```bash
chmod +x scripts/update.sh
./scripts/update.sh
```

更新脚本会执行：

1. 检查当前目录是否为 Git 仓库
2. 检查 `.env` 是否存在
3. 备份 `data/*.sqlite3*` 到 `backups/`
4. `git pull --ff-only`
5. `docker compose up -d --build`
6. 清理无用 Docker 镜像

业务数据在 `data/` 中，不会因为容器重建而丢失。

## 常用维护命令

查看容器状态：

```bash
docker compose ps
```

查看日志：

```bash
docker compose logs -f
```

重启：

```bash
docker compose restart
```

停止：

```bash
docker compose down
```

重新构建：

```bash
docker compose up -d --build
```

备份数据库：

```bash
mkdir -p backups
tar -czf backups/data-$(date +%Y%m%d-%H%M%S).tar.gz data/*.sqlite3*
```

## 本地开发

Windows PowerShell：

```powershell
.\scripts\dev.ps1
```

默认本地开发账号：

- 用户名：`admin@example.com`
- 密码：`admin-dev-password`

生产账号密码不要写入代码仓库，应通过 `.env` 设置。

## 后台路径安全

后台路径不要写死在代码或公开文档里。项目通过 `.env` 的 `ADMIN_PATH` 配置后台入口：

```env
ADMIN_PATH=/替换成你自己的后台路径
```

后台登录地址为：

```text
https://你的域名 + ADMIN_PATH + /login
```

示例：如果 `ADMIN_PATH=/my-panel-2026`，后台就是：

```text
https://你的域名/my-panel-2026/login
```

不要把真实 `ADMIN_PATH` 提交到 Git。生产服务器只需要把它写在 `.env` 里。隐藏后台路径只能减少扫描噪音，不能替代强密码、HTTPS、备份、最小权限和必要的访问控制。

## 支付接入策略

当前 `mock` 是模拟支付，只用于测试自动发卡流程。

真实支付宝/微信/聚合支付接入需要：

- 商户号
- 应用 ID
- API 密钥或证书
- 支付回调地址
- 支付返回地址
- 签名和验签规则
- 金额校验
- 幂等处理

后台支付通道配置页可以保存这些参数，但网关适配器代码仍需要按具体支付服务商文档实现。

不建议把错报类目或规避风控作为系统设计的一部分。实际申请类目应尽量和真实业务一致。

## 自动发卡流程

1. 用户选择商品并填写联系邮箱。
2. 系统创建订单并预占对应数量卡密。
3. 用户进入收银台。
4. 支付成功回调进入系统。
5. 系统验签、校验金额并执行发卡。
6. 预占卡密改为已售。
7. 用户在订单页或订单查询页看到卡密。

## GitHub 上传

首次上传建议使用 GitHub CLI：

```bash
gh auth login
gh repo create RHCloud1/Card-issuance --public --source=. --remote=origin --push
```

如果仓库名必须显示为 `Card issuance`，GitHub URL 中仍会转成类似 `Card-issuance` 的路径；仓库展示名可以在 GitHub 网页设置里调整。

## 参考

业务逻辑参考了 dujiaoka 的商品、卡密、订单和支付回调模型，但没有 fork 旧项目。支付层采用统一适配器设计，避免每个网关散落一套发货逻辑。
