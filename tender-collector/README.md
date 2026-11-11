# 市政工程投标截止时间采集服务

面向招投标专员的公告采集与资格核验 API：提交市政工程公告站的**列表页地址**，
服务自动遍历所有分页与详情页，将**更正公告按其引用的原公告编号归并**到同一
项目，并**按发布时间**（而非页面排列顺序）确定当前生效的投标截止时间，标记
是否已过期。采集完成后，可提交企业证书核验其是否满足原公告指定标段的全部
资格条件。

## 核心规则

1. **分页遍历**：从提交的列表页出发，沿 `nav.pagination` 链接抓取全部列表页
   （去重、防环、限量），收集所有详情页链接后逐页抓取。
2. **更正归并**：更正公告通过"原公告编号"归并到对应原公告所在项目；引用悬空
   （原公告不存在/不是原公告）视为数据错误，本次采集整体失败。
3. **生效截止时间**：同一项目内按**发布时间**排序，取发布时间最新且声明了
   截止时间的公告；发布时间相同时按公告编号确定次序，结果与页面顺序完全无关。
   若最新更正公告未给出截止时间（例如只改联系人），沿用上一份带截止时间的公告。
4. **过期判定**：`is_expired = 截止时间 <= 当前时间`（截止时间按服务器本地时区
   的 naive 时间比较，与公告页 `YYYY-MM-DD HH:MM` 格式一致）。
5. **幂等采集**：公告以编号为唯一键 UPSERT，重复采集不产生重复记录。
6. **失败保留**：任一列表页/详情页读取失败（网络错误、非 2xx、字段缺失）或
   归并校验失败时，本次采集不写入任何公告数据，**完整保留上次成功采集结果**；
   失败原因记入 `collection_runs` 表并通过 HTTP 422 返回。
7. **资格核验**：以已采集的**原公告编号** + **标段号**定位原公告详情页中的
   标段资格表，逐组核验企业证书。每组满足任一候选即可，所有组均须满足；
   同类别证书等级数字越小越高；证书有效期不得早于**更正归并后**的投标截止
   日期。详情页读取失败、资格表/标段/候选字段缺项时返回“无法判定”，不会
   误判为不满足。

## 目录结构

```
tender-collector/
├── app/                    # FastAPI 应用
│   ├── main.py             # 路由：/api/collect、/api/projects、/api/collections、
│   │                       #       /api/qualifications/verify
│   ├── service.py          # 采集编排 + 事务边界 + 过期判定 + 资格核验编排
│   ├── scraper.py          # httpx + BeautifulSoup 抓取/解析
│   ├── merge.py            # 更正归并与生效截止时间判定（纯函数）
│   ├── qualification.py    # 资格表解析 + 证书逐组判定（纯函数）
│   ├── database.py         # SQLite 连接、建表、UPSERT
│   ├── models.py           # Pydantic 模型
│   └── config.py           # 环境变量配置
├── demo_site/              # 生成的本地分页 HTML 示例公告站（静态文件）
├── scripts/generate_demo_site.py  # 示例站生成器（数据可复现）
├── tests/test_collection.py       # 端到端 + 归并单元测试
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 快速开始（Docker Compose）

```bash
# 1. 生成示例公告站静态文件（已随仓库提供，可重复生成）
python3 scripts/generate_demo_site.py

# 2. 启动：API 在 :8000，示例公告站在 :8001
docker compose up --build -d

# 3. 提交列表页地址触发采集
curl -X POST http://localhost:8000/api/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://demo-site/list_1.html"}'

# 4. 查询项目当前截止时间
curl http://localhost:8000/api/projects

# 5. 核验企业证书是否满足原公告某标段（001 一标段允许公路资质替代）
curl -X POST http://localhost:8000/api/qualifications/verify \
  -H 'Content-Type: application/json' \
  -d '{
    "announcement_code": "SZ-2026-001",
    "section_no": "1",
    "certificates": [
      {"category": "市政公用工程施工总承包", "level": 2, "valid_until": "2026-11-25"},
      {"category": "市政公用工程专业注册建造师", "level": 1, "valid_until": "2026-11-25"}
    ]
  }'
```

容器内 API 通过 compose 网络访问示例站（主机名 `demo-site`）；从宿主机浏览
示例站则打开 <http://localhost:8001/list_1.html>。SQLite 数据存于命名卷
`tender-data`（容器内 `/data/tenders.db`）。采集与资格核验均只访问 compose
内的本地示例站，**可离线运行**；验证资格表会实时读取原公告详情页。

停止与清理：

```bash
docker compose down          # 停止（保留数据卷）
docker compose down -v       # 停止并删除采集数据
```

## 本地开发运行（不使用 Docker）

```bash
python3 -m pip install -r requirements-dev.txt
python3 scripts/generate_demo_site.py

# 终端 A：示例公告站
python3 -m http.server 8001 --directory demo_site

# 终端 B：API（数据库默认在 ./data/tenders.db）
uvicorn app.main:app --reload --port 8000

# 终端 C：测试
pytest -q

# 采集
curl -X POST http://localhost:8000/api/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://127.0.0.1:8001/list_1.html"}'
```

交互式 API 文档（Swagger UI）：<http://localhost:8000/docs>

## API 说明

### `POST /api/collect` — 提交列表页，触发采集

请求：

```json
{ "list_url": "http://demo-site/list_1.html" }
```

成功响应（200）：

```json
{
  "status": "success",
  "source_url": "http://demo-site/list_1.html",
  "collected": 10,
  "projects": [
    {
      "project_name": "城北污水处理厂扩建工程",
      "current_deadline": "2026-11-30 10:00",
      "deadline_source_code": "SZ-2026-002-G2",
      "deadline_source_url": "http://demo-site/SZ-2026-002-G2.html",
      "is_expired": false
    }
  ]
}
```

失败响应（422，任一页面不可读/数据无法归并）：

```json
{ "detail": "读取页面失败 http://demo-site/SZ-2026-002-G2.html: HTTP 404" }
```

### `GET /api/projects` — 查询全部项目的当前截止时间

返回字段：项目名称、当前生效截止时间、截止时间来源公告编号、来源链接、是否过期。

### `GET /api/collections?limit=20` — 采集运行历史

包含每次采集的成功/失败状态、采集条数与错误信息。

### `POST /api/qualifications/verify` — 核验企业资质

请求传入**已采集的原公告编号**、**标段号**及企业证书（类别、等级、有效期）。
服务实时读取该原公告详情页中的标段资格表，并使用与 `/api/projects` 相同的
**更正归并后**投标截止时间进行判定。

```json
{
  "announcement_code": "SZ-2026-001",
  "section_no": "1",
  "certificates": [
    {
      "category": "市政公用工程施工总承包",
      "level": 2,
      "valid_until": "2026-11-25"
    },
    {
      "category": "市政公用工程专业注册建造师",
      "level": 1,
      "valid_until": "2026-11-25"
    }
  ]
}
```

说明：

- `section_no` 支持 `1`、`01`、`G1`、`一标段`、`标段一` 等写法；
- 也可用顶层单证书简写：`category` / `level` / `valid_until`，或
  `certificate` 对象；`level` 支持 `2`、`"2"`、`"二级"`；
- `valid_until` 支持 `YYYY-MM-DD`、`YYYY-MM-DD HH:MM` 等格式，按**日期**
  比较（有效期当日仍有效）。

成功判定（200）：

```json
{
  "status": "satisfied",
  "verdict": "满足",
  "can_satisfy": true,
  "announcement_code": "SZ-2026-001",
  "section_no": "1",
  "section_name": "一标段",
  "matched_section_no": "1",
  "project_name": "滨河路市政改造工程",
  "deadline": "2026-11-25 14:30",
  "deadline_source_code": "SZ-2026-001-G1",
  "deadline_source_url": "http://demo-site/SZ-2026-001-G1.html",
  "detail_url": "http://demo-site/SZ-2026-001.html",
  "groups": [
    {
      "group": "施工资质",
      "clause_source": "招标公告第三章 资格审查办法 2.1",
      "satisfied": true,
      "matched_candidate": {
        "category": "市政公用工程施工总承包",
        "level": 2,
        "level_text": "2级",
        "text": "市政公用工程施工总承包二级及以上"
      }
    }
  ],
  "unmet_conditions": [],
  "reason": null
}
```

未满足时 `status` 为 `unsatisfied`、`can_satisfy` 为 `false`，
`unmet_conditions` 逐项给出未满足的组、要求、原因及 `clause_source`（条款来源）：

```json
{
  "status": "unsatisfied",
  "verdict": "不满足",
  "can_satisfy": false,
  "unmet_conditions": [
    {
      "group": "项目经理资格",
      "clause_source": "招标公告第三章 资格审查办法 2.2",
      "requirement": "满足任一：市政公用工程专业注册建造师一级及以上",
      "candidates": [
        {
          "category": "市政公用工程专业注册建造师",
          "level": 1,
          "level_text": "1级",
          "text": "市政公用工程专业注册建造师一级及以上"
        }
      ],
      "reason": "企业证书类别未覆盖本组任一候选条件"
    }
  ]
}
```

判定规则：

- **每组满足任一候选即可，所有组均须满足**；替代资质（如公路工程、水利
  水电工程施工总承包）与主资质同组并列，任一即可；
- **等级数字越小越高**：候选“二级及以上”接受一级、二级，拒绝三级；
- 证书有效期必须 **>=** 生效投标截止日期（含同日）；
- 原公告未采集或编号不是原公告：HTTP 404；
- **详情页读取失败、资格表不存在、标段不存在、候选缺类别/等级时**：
  `status` 为 `undetermined`、`can_satisfy` 为 `null`，并在 `reason` 中说明，
  **不会误判为不满足**。即使详情页读取失败，只要归并截止时间可得仍会返回。

## 示例站数据设计

`demo_site/` 含 5 个项目、10 条公告、3 个列表页，列表按发布时间**倒序**排列
（更正公告排在原公告之前），专门用于验证归并不受页面顺序影响：

| 项目 | 场景 | 生效截止时间来源 |
| --- | --- | --- |
| 滨河路市政改造工程 | 一次更正延期 | 更正公告 SZ-2026-001-G1 |
| 城北污水处理厂扩建工程 | 两次更正，取最新一次 | 第二次更正 SZ-2026-002-G2 |
| 东风桥加固维修工程 | 无更正，已过期 | 原公告 SZ-2026-003 |
| 高新区雨污分流管网工程 | 更正不改截止时间 | 原公告 SZ-2026-004 |
| 湖滨公园配套市政道路工程 | 原公告在末页、更正在首页（跨页乱序） | 更正公告 SZ-2026-005-G1 |

资格核验示例数据（写在原公告详情页的 `table.qualification-table` 中）：

| 原公告 | 标段 | 资格条件 | 说明 |
| --- | --- | --- | --- |
| SZ-2026-001 | 1（一标段） | 施工资质：市政公用工程施工总承包二级及以上**或**公路工程施工总承包二级及以上（替代）；项目经理：市政公用工程专业注册建造师一级及以上 | 跨标段 + 替代资质 |
| SZ-2026-001 | 2（二标段） | 施工资质：市政公用工程施工总承包二级及以上**或**环保工程专业承包二级及以上（替代）；项目经理：市政公用工程专业注册建造师一级及以上 | 与一标段条件不同 |
| SZ-2026-002 | 1（一标段） | 施工资质：市政公用工程施工总承包一级及以上**或**水利水电工程施工总承包二级及以上（替代） | 替代资质 |
| SZ-2026-003/004/005 | — | 原公告未提供资格表 | 用于验证“无法判定” |

SZ-2026-001 的生效截止时间为更正后的 `2026-11-25 14:30`。因此证书有效期
`2026-11-20` 不满足（尽管它晚于原公告的 `2026-11-05`），`2026-11-25`
当日仍满足。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TENDER_DB_PATH` | 本地 `./data/tenders.db`；容器内 `/data/tenders.db` | SQLite 文件路径 |
| `TENDER_HTTP_TIMEOUT` | `15` | 单个页面请求超时（秒） |
| `TENDER_MAX_LIST_PAGES` | `200` | 单次采集最多遍历的列表页数（防环） |
| `TENDER_USER_AGENT` | `TenderCollector/1.0 ...` | 请求 UA |

## 解析约定（接入真实站点时）

解析器依赖以下稳定选择器，适配其他站点时调整 `app/scraper.py` 即可：

- 列表页：`ul.announcement-list a.detail-link` 为详情链接，
  `nav.pagination a` 为分页链接；
- 详情页：`article.announcement-detail` 容器，支持
  `data-code` / `data-kind` / `data-published-at` / `data-corrects` 属性，
  以及 `table.detail-table` 内 `th[data-field="字段名"] + td` 表格字段
  （公告编号、公告类型、项目名称、发布时间、投标截止时间、原公告编号）。
- 资格表：`table.qualification-table`，以 `data-section`（标段号）/
  `data-section-name`（标段名称）标识标段；表内 `tr.qual-group` 为一个
  资格条件组，`data-group` 为组名、`data-clause` 为条款来源；组内
  `span.candidate` 为候选条件，`data-category` 为证书类别、`data-level`
  为门槛等级（数字越小等级越高）。解析器也兼容把类别/等级写在候选文本中
  （如“市政公用工程施工总承包二级及以上”）的写法。
