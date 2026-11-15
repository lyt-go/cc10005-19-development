# 多源异构数据接入引擎

面向多源异构数据接入、智能代理调度、动态限速、爬虫深度管理、数据去噪、格式转换与采集质量监控的数据接入工程管理平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 连接器管理 | `connector` | 数据连接器 | 连接器编号、协议类型、目标端点 |
| 代理池管理 | `proxypool` | 代理实例 | 实例编号、代理地址、代理端口 |
| 深度爬取 | `deepcrawl` | 爬虫配置 | 配置编号、种子URL、最大深度 |
| JS渲染采集 | `jsrender` | 渲染配置 | 配置编号、目标URL、浏览器类型 |
| 字段解析器 | `parser` | 解析模板 | 模板编号、目标字段数、XPath表达式 |
| 数据去噪 | `dataclean` | 去噪规则 | 规则编号、目标字段、HTML标签清理 |
| 清洗批次 | `cleaning` | 清洗批次 | 批次编号、模板版本、去噪规则版本 |
| 格式转换 | `formatter` | 转换器 | 转换器编号、输入格式、输出格式 |
| 采集去重 | `dedupengine` | 去重引擎 | 引擎编号、去重字段组、哈希算法 |
| 动态限速 | `throttle` | 限速规则 | 规则编号、目标域组、初始速率 |
| 验证码处理 | `captcha` | 验证码策略 | 策略编号、验证码类型、识别服务商 |
| 采集监控 | `monitor` | 监控面板 | 面板编号、监控指标集、采样间隔秒 |
| 数据分发 | `exporttask` | 分发任务 | 任务编号、数据源标识、目标系统 |
| 种子管理 | `seedmgr` | URL种子 | 种子编号、种子URL、发现来源 |
| 站点地图 | `sitemap` | 站点描述 | 站点编号、站点域名、robots规则 |
| Cookie管理 | `cookiejar` | 会话凭证 | 凭证编号、所属站点、登录方式 |
| 采集器健康 | `healthcheck` | 健康报告 | 报告编号、采集器名称、CPU占用率 |
| 采集快照 | `snapshot` | 数据快照 | 快照编号、关联任务、快照时间 |
| 操作审计 | `audittrail` | 审计记录 | 记录编号、操作类型、操作对象 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA


## 清洗批次与文件交付

字段解析器与数据去噪各自保存生效状态，但「提交解析模板」会生成一个**清洗批次**，
把当时的口径全部冻结，之后规则改版只影响新批次：

- 冻结内容：目标字段、字段类型、XPath（含记录容器 XPath）、正则匹配式、类型转换规则、
  默认填充值、多值分隔符，以及各组件版本和去噪规则版本。
- 结果：`解析字段结果` 与 `清洗结果` 按 `(字段名, 记录序号)` 一一对应；
  多值字段带分隔符版本、逐项序列与项数；空值带可解释原因（XPath 未命中 / 文本为空 / 类型转换失败 /
  去噪后为空 / 默认填充值补齐）。
- 冲突裁决：同一字段命中多条去噪规则时，依次按「规则优先级 → 脱敏用途 → 规则版本新旧 →
  规则编号升序」确定唯一生效规则，并在批次里记录每条候选的裁决理由。
- 重复提交：同一模板、同一冻结口径按指纹判重，返回已有批次，不产生重复批次。
- 失败原子性：解析与清洗两侧先算后写，同一事务落库；任何一步失败整批回滚，
  不会留下只更新一侧的字段类型。测试可用 `CLEANING_FAILURE_INJECTION=fail_after` 验证。
- 默认填充值与敏感词脱敏的优先级：先执行含脱敏在内的全部去噪步骤，再应用默认填充值；
  填充值来源优先目标字段，其次生效去噪规则。这样填充值不会被脱敏后又写回明文。

接口：

- `POST /api/parser/{id}/submit`：提交解析模板并生成批次（幂等）。
- `GET /api/cleaning/batches`、`GET /api/cleaning/batches/{id}`
- `GET /api/cleaning/batches/{id}/field-results`、`GET /api/cleaning/batches/{id}/clean-results`
- `GET /api/cleaning/batches/{id}/verify`：核对两侧行键、多值项数与规则是否改版。
- `GET /api/delivery/batches/{id}/export|download|package`：导出 JSON 清单、下载结果 CSV、打包 zip。
- `GET /api/delivery/batches/{id}/manifest`：页面内直接查看交付清单。
- `GET /api/delivery/deliveries`：交付记录，每条带批次编号、模板版本与指纹。

交付物全部从批次冻结快照生成，因此导出/下载/打包里的解析模板、去噪规则、目标字段与
当前批次结果一致；规则改版后再导出旧批次，文件仍保持旧版本口径。zip 包内包含
`manifest.json`、`field-results.csv`、`clean-results.csv`、`versions.csv`（含 XPath、
正则、默认填充值、分隔符、脱敏各组件版本）、`conflicts.csv`、`checksums.json`。

版本格式为 `<作用域>.<组件>@V<n>`，例如 `PARS-0002.价格.XPATH@V2`、
`DATA-0003.REGEX@V1`。修改字段或规则时只递增变化组件的版本，并让模板/规则版本同步递增。

回归测试：

```bash
cd backend && .venv/bin/python tests/test_cleaning_batches.py
```
