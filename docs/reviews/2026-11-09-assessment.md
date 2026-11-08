Card-issuance 开发状态评估

评估日期：2026-11-09（北京时间）。对象：[RHCloud1/Card-issuance](https://github.com/RHCloud1/Card-issuance)，评估时的 main 提交 `c3055fc1d8a68e05d76b4a315013b53275e59a84`。本文件保留修复前的检查结果；后续本地改进另见同目录的改进记录。

结论：这是有前后台和基础库存交易逻辑的 v0.1 单商户发卡原型，可以演示“导入卡密—下单—模拟付款—发卡”。真实收款尚未实现，且已复现取卡授权、模拟支付隔离和异常库存处理问题，当前不适合正式收款发卡。

未找到原始开发聊天或单独的完整业务需求书。以下以仓库 README、架构文档和代码为验收依据；Xboard 联动另作条件判断，不假定它一定属于原始需求。

**开发进度**

仓库共 4 个提交：2026-06-16（北京时间）完成初始系统及后台路径配置；2026-07-29、2026-07-31 继续优化 VPS 部署。最近提交为“Improve VPS deployment reliability”，后续提交主要改善部署，没有完成真实支付网关。检查时没有 GitHub Actions 运行记录、Release 或 Issue 记录。

| 能力 | 当前实现 | 判断 |
| --- | --- | --- |
| 商品前台 | 分类筛选、价格排序、库存、描述、亮暗模式、匿名购买 | 已实现基础功能 |
| 商品和分类后台 | 新增、编辑、改价、排序、启停、上下架、单次购买上限 | 已实现基础功能 |
| 卡密管理 | 每行一条导入、同商品内去重、状态统计、作废 | 已实现基础功能；无生成、导出、搜索和分页 |
| 订单及库存 | 整数分金额、随机订单号、事务预占、过期释放、模拟履约 | 正常流程可运行，异常恢复不足 |
| 订单查询 | 邮箱查询最近 50 条，或邮箱加订单号；可选取卡密码 | 已实现，但密码可被另一订单页面绕过 |
| 支付 | mock 可发货；支付宝、微信、聚合支付配置可保存 | 真实支付未接入 |
| 工单 | 买家提交，后台查看最近 200 条 | 缺少回复、状态处理、关闭及买家追踪 |
| 订单运营 | 最近 200 条只读列表 | 缺少搜索、分页、退款、补单、补发及异常处理 |
| 部署 | Docker、交互配置、Caddy HTTPS、更新、诊断、数据持久化 | 有部署基础，仍有可靠性问题 |
| Xboard | 未发现相关 API 或业务逻辑 | 不支持自动开户、充值、开通套餐或续费 |

功能范围及真实支付边界也已在 [README](https://github.com/RHCloud1/Card-issuance/blob/c3055fc1d8a68e05d76b4a315013b53275e59a84/README.md) 中说明。

**正式使用前需要处理的问题**

1. **阻断：真实收款链路缺失。** 所有 `/payments/<provider_code>/notify` 请求直接返回 501；非 mock 收银页只提示尚未接入。没有真实下单、二维码/跳转、回调验签和支付对账。保存商户 JSON 不会让支付生效。已有金额比较、交易号唯一约束和统一发货函数，但这不能代替网关适配。[回调占位代码](https://github.com/RHCloud1/Card-issuance/blob/c3055fc1d8a68e05d76b4a315013b53275e59a84/app/main.py#L198)，[收银页](https://github.com/RHCloud1/Card-issuance/blob/c3055fc1d8a68e05d76b4a315013b53275e59a84/app/templates/checkout.html#L35)。

2. **严重，已复现：取卡密码保护可绕过。** 陌生客户端用下单邮箱和错误密码查询，页面仍显示订单号；再访问 `/checkout/<order_no>`，无需密码便能得到已发货卡密。查询页和收银页没有统一的买家取卡授权。[查询与收银路由](https://github.com/RHCloud1/Card-issuance/blob/c3055fc1d8a68e05d76b4a315013b53275e59a84/app/main.py#L169)，[卡密直接输出](https://github.com/RHCloud1/Card-issuance/blob/c3055fc1d8a68e05d76b4a315013b53275e59a84/app/templates/checkout.html#L40)。

3. **严重，已复现：关闭 mock 后仍能免费发卡。** 禁用 mock、启用 alipay 后创建的 alipay 订单，仍能经匿名模拟成功接口变为 fulfilled 并交付卡密。CSRF 令牌可由普通访客正常取得，不能证明付款。模拟路由未检查运行环境、mock 启用状态和订单支付通道；统一履约函数也没有通道匹配校验。生产环境应禁用模拟路由，并统一校验发货来源。[模拟路由](https://github.com/RHCloud1/Card-issuance/blob/c3055fc1d8a68e05d76b4a315013b53275e59a84/app/main.py#L184)，[履约逻辑](https://github.com/RHCloud1/Card-issuance/blob/c3055fc1d8a68e05d76b4a315013b53275e59a84/app/main.py#L805)。

4. **已复现：已预占卡密可以被后台作废，导致异常订单和库存滞留。** 两张卡的待支付订单中作废一张，再付款，订单进入 abnormal，另一张仍 reserved。即使将过期时间改到过去，过期任务也不会释放它，因为只扫描 pending。后台没有修复、补发或退款入口。应约束预占卡密操作，并补异常订单的库存与售后处理。[作废逻辑](https://github.com/RHCloud1/Card-issuance/blob/c3055fc1d8a68e05d76b4a315013b53275e59a84/app/main.py#L456)，[异常状态处理](https://github.com/RHCloud1/Card-issuance/blob/c3055fc1d8a68e05d76b4a315013b53275e59a84/app/main.py#L871)，[过期释放](https://github.com/RHCloud1/Card-issuance/blob/c3055fc1d8a68e05d76b4a315013b53275e59a84/app/main.py#L625)。

5. **已复现初始化竞态：首次多进程启动不可靠。** Docker 使用 Gunicorn 两个 worker，各 worker 创建应用时都会初始化数据库和默认管理员。两个独立 Python 子进程同时初始化全新数据库，重复 5 次，每次均有一个失败：3 次管理员唯一约束冲突、2 次 database is locked。应在启动 worker 前单独完成幂等初始化。这是本地 Windows/Python 3.13 的复现，未直接验证 Linux Gunicorn 的最终启动结果。[Docker 启动命令](https://github.com/RHCloud1/Card-issuance/blob/c3055fc1d8a68e05d76b4a315013b53275e59a84/Dockerfile#L18)，[初始化逻辑](https://github.com/RHCloud1/Card-issuance/blob/c3055fc1d8a68e05d76b4a315013b53275e59a84/app/main.py#L558)。

6. **已复现：部署脚本可能改变含 `$` 的管理员密码。** 用户输入原样写入未加引号的 `.env`，Docker Compose 会再次展开变量。本机只解析 Compose 配置时，测试字符串 `Sample$REVIEW_UNDEFINED_837462` 最终变成 `Sample`。原密码可能无法登录，实际密码也可能变弱。应正确保存字面值或使用 secret 文件。[写入 .env](https://github.com/RHCloud1/Card-issuance/blob/c3055fc1d8a68e05d76b4a315013b53275e59a84/scripts/setup.sh#L376)。

7. **静态确认的运维缺口：** 更新脚本对运行中的 SQLite WAL 数据直接 tar，不保证一致性恢复；没有版本化结构迁移、更新健康检查和失败回滚。安装脚本声称支持 Debian，却固定使用 Ubuntu Docker 源，Debian 首次安装可能失败。备份应使用 SQLite online backup 或停写快照，并实际演练恢复。[更新脚本](https://github.com/RHCloud1/Card-issuance/blob/c3055fc1d8a68e05d76b4a315013b53275e59a84/scripts/update.sh#L31)，[数据库初始化](https://github.com/RHCloud1/Card-issuance/blob/c3055fc1d8a68e05d76b4a315013b53275e59a84/app/db.py#L33)，[Docker 安装源](https://github.com/RHCloud1/Card-issuance/blob/c3055fc1d8a68e05d76b4a315013b53275e59a84/scripts/install-docker-ubuntu.sh#L27)。

**本次验证范围**

- 本地隔离功能检查 19 项通过：后台访问限制、CSRF、管理员登录、分类及商品创建、卡密导入去重、前台渲染、订单创建、分单位金额、库存预占、库存不足回滚、模拟发货、重复履约不重复出货、正确密码查询、工单提交、后台页面渲染、过期释放，以及并发库存分配。
- 12 个并发请求争抢 5 张卡，恰好 5 个订单成功，各自绑定不同卡密。这验证了小规模并发下的库存原子分配，不代表大规模负载能力。
- 6 个运维脚本及部署测试脚本静态语法检查通过；仓库原有部署脚本测试 19 个用例通过，使用 fake Docker、SS、curl。
- 以上安全缺陷、异常库存、初始化竞态及密码插值分别做了隔离复现。
- 本地功能测试使用 Python 3.13.14、Flask 3.1.3；项目 Docker 镜像定义为 Python 3.12。未运行真实 Linux VPS 部署、真实支付、邮件发送、证书申请或恢复演练。
- 评估阶段未修改仓库源码、未 commit、未 push。项目后续已迁移至 [Card-issuance](E:/Project/Python/Card-issuance)，原功能检查脚本保存在 [.review-artifacts/baseline_checks.py](E:/Project/Python/Card-issuance/.review-artifacts/baseline_checks.py)。

**是否满足需求**

| 目标 | 当前判断 |
| --- | --- |
| 展示一个前后台完整雏形，演示模拟购买与发卡 | 可以，正常流程已实测 |
| 单商户导入预先准备的文本卡密后真实自动收款发货 | 尚不满足，先完成支付及安全修复 |
| 作为继续开发的基础 | 可以，现有库存事务和后台可复用 |
| 完整售后、补发、退款、异常处理和长期稳定运营 | 尚不满足 |
| 支付后自动给 Xboard 开账号、开套餐、充值或续费 | 未实现；如仅销售事先生成的文本内容，属于普通发卡用途 |

建议先明确要接入的具体支付网关，修复取卡授权、生产模拟支付隔离和库存异常处理，再做支付沙箱端到端验收；随后处理初始化、密码保存、可靠备份，以及所需的售后能力。若目标包含 Xboard 自动开通，则还需要单独设计和实现联动流程。
