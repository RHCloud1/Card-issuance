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
scripts/deploy.sh     首次部署脚本
scripts/update.sh     后续一键更新脚本
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

## 安装 Docker

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

## 首次部署

克隆仓库：

```bash
git clone https://github.com/RHCloud1/Card-issuance.git card-issuance
cd card-issuance
```

创建配置文件：

```bash
cp .env.example .env
```

编辑 `.env`：

```bash
nano .env
```

至少修改以下配置：

```env
APP_NAME=Card Issuance
APP_SECRET=替换为一段很长的随机字符串
APP_BASE_URL=https://你的域名
DATABASE_PATH=/data/card_issuance.sqlite3
ADMIN_PATH=/替换成你自己的后台路径
ADMIN_USERNAME=你的后台邮箱
ADMIN_PASSWORD=你的后台强密码
ORDER_EXPIRE_MINUTES=15
```

生成 `APP_SECRET` 示例：

```bash
openssl rand -base64 48
```

启动：

```bash
chmod +x scripts/deploy.sh
./scripts/deploy.sh
```

访问：

- 前台：`http://服务器IP:8080/`
- 后台：`http://服务器IP:8080` + `.env` 中的 `ADMIN_PATH` + `/login`

## 宝塔或 Nginx 反向代理

推荐让 Docker 只监听本机 `8080`，由宝塔或 Nginx 负责域名和 HTTPS。

反向代理目标：

```text
http://127.0.0.1:8080
```

Nginx 示例：

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
