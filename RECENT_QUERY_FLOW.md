# `recent` 查询流程说明（单次探测之外的历史读取）

本说明描述 `healthcheck.py` **当前已实现**的 `recent` 子命令流程（含 `--url`、
`--status`、`--reason`、`--since` 四个可选筛选），不新增任何行为。内容从公开
命令行参数进入，沿「参数校验 → 数据库只读查询 → JSON 输出」讲清每一步，并标注
结论对应的函数或 SQL 常量（行号以当前 `healthcheck.py` 为准）。
文中第 3–9 节的验收示例已分别用 `test_recent_summary.py`（7 条固定样本）、
`test_recent_status_filter.py`（5 条验收样本）与
`test_recent_reason_filter.py`（5 条 reason 验收样本）实际运行核对，退出码、
stdout、stderr 均与记录一致；现有测试套件（149 项，3 项跳过）全部通过。
第 10–11 节的 `--since` 示例复用 `test_recent_since.py` 的四条固定样本，
其中给出的输出是**按该样本推定的预期结果**（可由该测试文件公开入口核对），
不沿用「已实际运行」的结论表述。
八种筛选组合此前各由一条独立 SQL 常量实现，现已重构为单一模板 `SELECT_SQL`
加 `build_recent_query` 动态组装（行为等价，回归验证见
`test_recent_refactor_regression.py`），本文按重构后的代码描述。

- 入口函数：`main`（`healthcheck.py:700`）→ `command_recent`（`healthcheck.py:488-622`）
- `recent` **不发送网络请求、不创建文件/目录/表、不改动已有记录**；数据库以只读
  URI 打开，可读但不可写的库仍可查询。

---

## 1. 公开参数

命令形式（参数定义见 `build_parser` 中 recent 子解析器，`healthcheck.py:643-695`）：

```sh
python healthcheck.py --db <数据库路径> recent \
    [--url <目标URL>] [--status {success,failure}] \
    [--reason {ok,http_status,connection_error,timeout}] \
    [--since <YYYY-MM-DDTHH:MM:SS[.ffffff](Z|+00:00)>] \
    [--limit <正整数>] [--summary]
```

| 参数 | 是否必填 | 默认 | 约束与语义 | 代码位置 |
|---|---|---|---|---|
| `--db` | 必填（全局） | 无 | SQLite 数据库文件路径 | `healthcheck.py:630` |
| `--url` | 可选 | `None` | 给出时只返回该目标的记录；省略时查询**全部目标** | `healthcheck.py:644-649` |
| `--status` | 可选 | `None`（不筛选） | 给出时只返回该状态的记录；**只接受区分大小写的 `success` / `failure`**，省略时查询全部状态 | `healthcheck.py:650-659` |
| `--reason` | 可选 | `None`（不筛选） | 给出时只返回保存的 reason 与之**精确相等**的记录；**只接受区分大小写的 `ok` / `http_status` / `connection_error` / `timeout`**，省略时查询全部原因；**不从状态码推断** | `healthcheck.py:660-670` |
| `--since` | 可选 | `None`（不筛选） | 给出时只返回 `checked_at` **不早于**该 UTC 时刻的记录；格式为 `YYYY-MM-DDTHH:MM:SS`（秒后可带一至六位小数）并以 `Z` 或 `+00:00` 结尾，日期时间须真实存在；省略时不增加时间条件、也不校验记录的 `checked_at` | `healthcheck.py:671-682` |
| `--limit` | 可选 | `5`（`DEFAULT_LIMIT`，`healthcheck.py:29`） | 正整数；`0`、负数、`1.5`、非数字均被拒绝 | `positive_limit`，`healthcheck.py:145-153` |
| `--summary` | 可选开关 | 关 | 输出这批记录的耗时摘要，而非记录数组 | `healthcheck.py:689-694` |

### 1.1 `--url` 的规则

`--url` 的合法规则与 `check` 完全一致，由 `validate_target_url`
（`healthcheck.py:300-350`）校验：仅 `http` 协议、主机必须是 `127.0.0.1`、
必须显式指定 `1-65535` 端口；允许路径与查询参数；不接受用户信息（userinfo），
原始字符串中只要出现 `#` 即按片段（fragment）拒绝；含空白/控制字符、
结构无法解析、主机或端口非法也一律拒绝。

**注意：校验只用于判定输入是否合法；数据库匹配使用的是命令行上的原始字符串，
不做任何规范化。**

### 1.2 `--status` 的规则

`--status` 由 `validate_status_filter`（`healthcheck.py:156-175`）校验：

- 取值只能是字符串 **`success`** 或 **`failure`**，**区分大小写**：
  `Success`、`FAILURE`、带前后空白（`" failure"`）等都不合法；
- **空字符串**（`--status ""`）不合法；
- **裸 `--status`（命令行上缺少值）** 不合法：argparse 以
  `nargs="?", const=STATUS_FILTER_MISSING`（`healthcheck.py:652-653`）把该
  情形标记为哨兵 `STATUS_FILTER_MISSING`（`healthcheck.py:41`），再由校验函数
  识别并拒绝（与「完全省略 `--status`」的 `None` 严格区分）；
- 以上非法情形一律经 `die`（`healthcheck.py:129-132`）输出 stderr 并以退出码
  `2` 结束，**stdout 为空**，且发生在 URL、reason、since 校验与一切数据库
  访问**之前**。

筛选只依据记录的 **`status` 字段**做等值匹配，**不区分失败原因**：
`reason` 为 `http_status` / `connection_error` / `timeout` 的记录在
`--status failure` 下同样命中。

### 1.3 `--reason` 的规则

`--reason` 由 `validate_reason_filter`（`healthcheck.py:178-201`）校验：

- 取值只能是字符串 **`ok`**、**`http_status`**、**`connection_error`**、
  **`timeout`** 四者之一，**区分大小写**：`OK`、`Timeout`、`HTTP_STATUS`、
  带前后空白（`" timeout"`、`"ok "`）等都不合法；
- **空字符串**（`--reason ""`，含纯空白 `"  "`）不合法；
- **裸 `--reason`（命令行上缺少值）** 不合法：argparse 以
  `nargs="?", const=REASON_FILTER_MISSING`（`healthcheck.py:662-663`）把该
  情形标记为哨兵 `REASON_FILTER_MISSING`（`healthcheck.py:48`），再由校验函数
  识别并拒绝（与「完全省略 `--reason`」的 `None` 严格区分）；
- 以上非法情形一律经 `die` 输出 stderr 并以退出码 `2` 结束，**stdout 为空**，
  且发生在 **status、URL 都合法之后、since 校验与一切数据库访问之前**——
  拒绝时不会读取、更不会创建数据库。
- 校验在 `command_recent` 中的调用点为 `healthcheck.py:505`。

筛选**只按数据库保存的 `reason` 字段做等值精确匹配，绝不从 `status` 或
`http_status` 推断**：例如 failure 记录的 reason 保存为 `timeout` 时，
`--reason http_status` 与 `--reason connection_error` 都不会命中它，
`--reason timeout` 也不会因为某条 success 记录的 `http_status` 恰好超时而命中
（success 记录的 reason 恒为 `ok`）。与 `--url`、`--status` 同用时三个条件
取交集（AND）；合法组合无匹配时返回空结果，退出码仍为 0。

### 1.4 `--since` 的规则

`--since` 由 `validate_since_filter`（`healthcheck.py:253-273`）校验，
接受的格式与记录的 `checked_at` 共用同一条严格 UTC 规则：

- **格式**：`YYYY-MM-DDTHH:MM:SS`，秒后可带**一至六位**小数，且必须以
  **`Z` 或 `+00:00`** 结尾。形状由 `UTC_TIMESTAMP_RE`（`healthcheck.py:66-68`）
  全串匹配判定；`UTC_TIMESTAMP_BODY_RE`（`healthcheck.py:72-74`）用于在匹配
  失败时区分「缺时区」「非 UTC 偏移」等具体原因。
- **日期时间须真实存在**：形状合法但日期时间不存在（如 `2026-02-30T00:00:02Z`、
  `25:00:00`）同样拒绝——由 `parse_utc_timestamp`
  （`healthcheck.py:204-225`）中的 `datetime` 构造抛出 `ValueError` 识别。
- **不合法情形**（均由 `since_format_problem`，`healthcheck.py:228-250`，
  给出中文原因）：**空值**（`--since ""`）、**前后带空白**、**缺少时区**
  （无 `Z`/`+00:00` 结尾）、**非 UTC 偏移**（如 `+08:00`）、**非法日期时间**、
  其他格式不符（如七位小数）。
- **裸 `--since`（命令行上缺少值）** 不合法：argparse 以
  `nargs="?", const=SINCE_FILTER_MISSING`（`healthcheck.py:673-674`）把该
  情形标记为哨兵 `SINCE_FILTER_MISSING`（`healthcheck.py:61`），再由校验函数
  识别并拒绝（与「完全省略 `--since`」的 `None` 严格区分）。
- 以上非法情形一律经 `die` 输出 stderr 并以退出码 `2` 结束，**stdout 为空**，
  stderr 信息以「since 参数错误」开头并说明具体原因、回显实际值；
  校验发生在 **status、URL、reason 都合法之后、一切数据库访问之前**——
  拒绝时不会读取、更不会创建数据库（即使数据库路径同时非法，也优先报
  since 错误，见第 12.2 节）。
- 校验在 `command_recent` 中的调用点为 `healthcheck.py:510`。

比较语义：**按时刻比较，不按字符串比较**。`parse_utc_timestamp` 把 `Z` 与
`+00:00` 都解析为 `tzinfo=timezone.utc` 的同一时刻；省略小数与显式零小数
等价（缺省小数按 `"0"` 处理并右补零到微秒，`healthcheck.py:216`），因此
`2026-10-04T00:00:02Z` 与 `2026-10-04T00:00:02.000000+00:00` 是同一时刻。
命中规则是**不早于起点**（`checked_at >= since`，`healthcheck.py:295`），
与起点相等的记录保留。与 `--url`、`--status`、`--reason` 同用时取交集；
**省略 `--since` 时不增加任何时间条件，也不校验记录的 `checked_at`**。

---

## 2. 端到端流程

`main` 解析参数后调用 `args.handler`，对 `recent` 即 `command_recent`
（`healthcheck.py:488-622`）。步骤严格按以下顺序发生：

1. **先校验 `--status`**（`healthcheck.py:494`）。
   提供了 `--status` 时先调用 `validate_status_filter`；非法值（含空字符串、
   裸 `--status` 缺值、大小写不符等）立即经 `die` 以退出码 `2` 结束。
   这一步先于 URL、reason、since 校验与一切数据库路径判断，因此 **status
   非法时优先报 status 错误**，即使同时给出非法 URL、非法 reason、非法
   since 或不存在/目录形式的数据库路径。

2. **再校验筛选 URL**（`healthcheck.py:499-500`）。
   提供了 `--url` 时调用 `validate_target_url(args.url)`；非法 URL 立即经
   `die` 以退出码 `2` 结束。status 合法后，**非法 URL 仍优先于 reason、
   since 与数据库路径错误**。仅做校验，匹配时仍使用原始字符串。

3. **再校验 `--reason`**（`healthcheck.py:505`）。
   提供了 `--reason` 时调用 `validate_reason_filter`；非法值（含空字符串、
   裸 `--reason` 缺值、大小写不符、前后空白等）立即经 `die` 以退出码 `2`
   结束。status 与 URL 都合法后，**非法 reason 仍优先于 since 与数据库路径
   错误**，且拒绝发生在数据库访问之前。

4. **再校验 `--since`**（`healthcheck.py:510`）。
   提供了 `--since` 时调用 `validate_since_filter`；非法值（裸 `--since`
   缺值、空值、前后空白、缺时区、非 UTC 偏移、非法日期时间等）立即经 `die`
   以退出码 `2` 结束。status、URL、reason 都合法后，**非法 since 仍优先于
   数据库路径错误**（目录、缺库等都不会再被报告），且拒绝发生在数据库访问
   之前。校验通过时返回用于按时刻比较的 UTC aware `datetime`；省略
   `--since` 时为 `None`，不增加时间条件。

5. **数据库路径不存在 → 视为空历史**（`healthcheck.py:514-519`）。
   `os.path.exists` 为假时（文件不存在，**连父目录一起不存在也相同**），
   普通查询输出 `[]`，摘要输出常量 `NULL_SUMMARY_JSON`
   （`healthcheck.py:123-126`），退出码 `0`。此分支不打开 sqlite，不创建任何东西。

6. **路径是目录 → 错误**（`healthcheck.py:522-523`）。
   经 `die` 报「路径是一个目录，不是 SQLite 数据库文件」，退出码 `2`。

7. **以只读模式打开数据库**（`healthcheck.py:527-531`）。
   打开的是文件绝对路径的 file URI 并附 `?mode=ro`：
   `pathlib.Path(...).as_uri() + "?mode=ro"`。因此：
   - 不执行 `CREATE TABLE`，不会创建或修改数据库文件，也不产生 `-wal`/`-journal`；
   - 权限为「可读不可写」的文件照样可以查询；
   - 无法打开（如读取权限不足）时经 `die` 结束，退出码 `2`。
   注意：`check` 使用的 `open_database`（`healthcheck.py:353-369`，会建库建表）
   **recent 从不调用**。

8. **识别历史表并执行查询**（`healthcheck.py:533-576`）。
   先查 `sqlite_master`，表名按**大小写不敏感**识别历史表：`CHECKS`、
   `Checks` 等与 `checks` 是同一历史表（SQLite 标识符本身大小写不敏感，
   这类异写表至多存在一个），查询时使用库中保存的实际表名（加引号）。
   有效库中没有历史表（空库或仅有其他表）时，`rows = []`，不报错、不建表。
   识别到历史表后，先用 `ensure_checks_columns`（`healthcheck.py:372-384`，
   与 `check` 探测前的结构校验同一函数）核对七个所需字段，**缺任意一个即
   经 `die` 报「checks 表缺少字段: …」以退出码 `2` 结束，不再误判为空历史**；
   字段名比较沿用 SQLite 的大小写不敏感语义，列顺序不同或存在额外列均可。
   字段齐全后，由 `build_recent_query`（`healthcheck.py:461-485`）按三个
   SQL 侧可选筛选（url/status/reason）的组合统一组装查询：模板只有一条
   `SELECT_SQL`（`healthcheck.py:106-112`），`{where}` 按当前组合由
   `RECENT_FILTER_CLAUSES`（`healthcheck.py:116-120`）中对应的条件片段
   （`url = ?` / `status = ?` / `reason = ?`，按 url → status → reason 的
   固定顺序以 AND 拼接）生成，无筛选时为空字符串；所有组合都是
   `ORDER BY id DESC`，选择列顺序都是
   `id, url, checked_at, elapsed_ms, status, http_status, reason`：

   | `--url` | `--status` | `--reason` | WHERE 子句 | 绑定参数（未给 `--since` 时） |
   |---|---|---|---|---|
   | 省略 | 省略 | 省略 | （无） | `(limit,)` |
   | 给出 | 省略 | 省略 | `WHERE url = ?` | `(url, limit)` |
   | 省略 | 给出 | 省略 | `WHERE status = ?` | `(status, limit)` |
   | 省略 | 省略 | 给出 | `WHERE reason = ?` | `(reason, limit)` |
   | 给出 | 给出 | 省略 | `WHERE url = ? AND status = ?` | `(url, status, limit)` |
   | 给出 | 省略 | 给出 | `WHERE url = ? AND reason = ?` | `(url, reason, limit)` |
   | 省略 | 给出 | 给出 | `WHERE status = ? AND reason = ?` | `(status, reason, limit)` |
   | 给出 | 给出 | 给出 | `WHERE url = ? AND status = ? AND reason = ?` | `(url, status, reason, limit)` |

   语义统一为**先用全部给定条件筛选（`url` 原始字符串精确匹配、`status` 与
   `reason` 各按保存值等值匹配，条件之间是 AND），再按 id 倒序取前
   `--limit` 条**：limit 永远作用在筛选之后，不会先截断再筛选。`reason`
   条件是 `WHERE reason = ?`，与 `http_status`、`status` 列无关，不做任何
   推断或重映射。

   **`--since` 不进入 SQL 条件**：`--since` 生效时 `build_recent_query` 以
   `sql_limit=False` 组装（调用点 `healthcheck.py:561-569`），SQL **不附加
   `LIMIT`**，先取出符合其余筛选的全部记录（仍按 id 倒序），再由
   `filter_rows_by_since`（`healthcheck.py:276-297`，调用点 `574-576`）在
   Python 侧逐条处理：
   - 每条记录的 `checked_at` 先经 `parse_utc_timestamp` 校验；**只要符合
     其余筛选的记录中存在格式非法的 `checked_at`，即经 `die` 以退出码 `2`
     结束，stderr 指出该记录的 id**——校验针对全部符合其余筛选的记录，
     在 limit 截取之前完成，因此坏记录即使按 id 倒序排在 limit 之外也照样
     被发现（不修改任何数据）；被其余筛选（url/status/reason）排除的记录
     不参与校验，其 `checked_at` 是否合法不影响查询；
   - 校验通过且**不早于起点**（`checked_at >= since`，按时刻比较）的记录
     保留，顺序不变（仍按 id 倒序）；
   - 最后 `kept[:limit]` 截取前 `--limit` 条——**先完成时间筛选，才限量**。

   查询期 sqlite 错误（文件不是有效 SQLite 库、读取失败等）
   由 `except sqlite3.Error` 捕获（`healthcheck.py:579-582`），经 `die` 以
   退出码 `2` 结束。

9. **输出 JSON**（单行，紧凑分隔，`ensure_ascii=False`，末尾一个换行）：
   - `--summary`：见第 5、11 节（`healthcheck.py:584-607`）；
   - 普通查询：把每行按七字段映射为记录对象数组
     （`healthcheck.py:609-620`），`json.dumps(..., separators=(",", ":"))`
     输出（`healthcheck.py:621`）。无记录时输出 `[]`。

两种成功形态退出码均为 `0`，stderr 为空。

---

## 3. 固定样本一（复用 `test_recent_summary.py`，7 条）

样本由测试夹具 `build_sample_db` 写入，共 7 条记录，涉及两个目标：

- 目标 A：`http://127.0.0.1:8765/health?detail=1`
- 目标 B：`http://127.0.0.1:8765/health?detail=2`（仅查询参数值不同）

| id | 目标 | elapsed_ms | status | http_status | reason | checked_at (UTC) |
|----|------|-----------|--------|-------------|--------|------------------|
| 1 | A | 90 | success | 200 | ok | 2026-10-02T04:40:06+00:00 |
| 2 | B | 80 | success | 200 | ok | 2026-10-02T04:40:05+00:00 |
| 3 | A | **0** | **failure** | null | connection_error | 2026-10-02T04:40:04+00:00 |
| 4 | B | 70 | success | 200 | ok | 2026-10-02T04:40:03+00:00 |
| 5 | A | 2 | success | 200 | ok | 2026-10-02T04:40:02+00:00 |
| 6 | A | 7 | **failure** | 500 | http_status | 2026-10-02T04:40:01+00:00 |
| 7 | B | 60 | success | 200 | ok | 2026-10-02T04:40:00+00:00 |

时间顺序是有意安排的：**`checked_at` 的新旧与 id 相反**——id 1 时间最新、
id 7 时间最早。这样可以证明排序只看 id（见第 4 节）。A 的 4 条记录为
id 1、3、5、6，其中 id 3（0 ms）与 id 6（7 ms）是 failure；B 的 3 条
（id 2、4、7）全部 success。

---

## 4. 验收示例一：普通查询（记录数组）

```sh
python healthcheck.py --db monitor.sqlite recent \
    --url http://127.0.0.1:8765/health?detail=1 --limit 3
```

实际输出（单行 JSON；退出码 `0`，stderr 为空）：

```json
[{"id":6,"url":"http://127.0.0.1:8765/health?detail=1","checked_at":"2026-10-02T04:40:01.000000+00:00","elapsed_ms":7,"status":"failure","http_status":500,"reason":"http_status"},{"id":5,"url":"http://127.0.0.1:8765/health?detail=1","checked_at":"2026-10-02T04:40:02.000000+00:00","elapsed_ms":2,"status":"success","http_status":200,"reason":"ok"},{"id":3,"url":"http://127.0.0.1:8765/health?detail=1","checked_at":"2026-10-02T04:40:04.000000+00:00","elapsed_ms":0,"status":"failure","http_status":null,"reason":"connection_error"}]
```

要点：

- **id 顺序为 6、5、3**，每条都保留完整七个字段（`id/url/checked_at/elapsed_ms/
  status/http_status/reason`），失败记录（含 `http_status: null`）原样返回。
- 语义是**先按原始 URL 精确筛选，再按 id 倒序取前 3 条**：
  `WHERE url = 'http://127.0.0.1:8765/health?detail=1'` 命中 A 的 id 1/3/5/6，
  `ORDER BY id DESC` 得 6、5、3、1，`LIMIT 3` 截掉 id 1。
  对应 `SELECT_SQL` 模板加 `WHERE url = ?` 条件
  （`healthcheck.py:106-112, 116-120`）。
- **`checked_at` 不决定排序**：输出里 id 6 的时间（04:40:01）早于 id 5
  （04:40:02）更早于 id 3（04:40:04），却排在最前——排序键只有 id。
- **路径或查询参数不同的目标不合并**：目标 B（`detail=2`）的 id 2/4/7 被
  `WHERE url = ?` 排除；`/health?detail=2`、`/health/`、`/ready`、
  `http://127.0.0.1:8765` 与 `http://127.0.0.1:8765/` 等都是不同的原始字符串，
  互不匹配（另见 `test_recent_url_filter.py` 的近似地址用例）。

## 5. 验收示例二：同一批记录的摘要

加上 `--summary`，其余参数完全相同：

```sh
python healthcheck.py --db monitor.sqlite recent \
    --url http://127.0.0.1:8765/health?detail=1 --limit 3 --summary
```

实际输出（单行 JSON；退出码 `0`，stderr 为空）：

```json
{"count":3,"min_elapsed_ms":0,"max_elapsed_ms":7,"avg_elapsed_ms":3.0}
```

**摘要统计口径确认**（实现：`healthcheck.py:584-607`）：摘要统计的就是
**同条件 `recent`（不带 `--summary`）会返回的同一记录子集**——同样的筛选
条件（含 `--reason`、`--since`）、同样按 id 倒序、同样的 limit 截取；代码对
这批 `rows` 直接取 `row[3]`（即 `elapsed_ms`）计算，并不另外发起任何查询。
因此它：

- **不是全历史统计**：id 1（90 ms）因 `LIMIT 3` 被排除，不计入；
- **不是时间窗口统计**：未给 `--since` 时没有任何时间条件，`checked_at`
  只作为输出字段；
- **不是仅成功记录**：id 6（failure，7 ms）与 id 3（failure，0 ms）都计入；
- **零耗时参与统计**：`min_elapsed_ms` 为 0（id 3），count 仍为 3；
- **平均值不取整**：`(7 + 2 + 0) / 3 = 3.0`，以 JSON 数字原样输出
  （`sum(...) / count` 的浮点除法，`healthcheck.py:595`），故序列化为 `3.0`
  而非 `3`；max 为 7（id 6）。

无记录时（缺库、无历史表、空表、筛选无匹配），摘要为
`count: 0` 且三个耗时字段为 `null`：缺库快路径输出常量 `NULL_SUMMARY_JSON`
（`healthcheck.py:123-126`），其余路径构造等价对象（`healthcheck.py:599-605`）。

---

## 6. 固定样本二（`test_recent_status_filter.py`，5 条验收样本）

该样本专门验收 `--status`，共 5 条记录，仍只涉及目标 A 与 B：

- A：`http://127.0.0.1:8765/health?detail=1`
- B：`http://127.0.0.1:8765/health?detail=2`（仅 `detail` 值不同）

| id | 目标 | elapsed_ms | status | http_status | reason | checked_at (UTC) |
|----|------|-----------|--------|-------------|--------|------------------|
| 1 | A | **0** | **failure** | null | connection_error | 2026-10-04T00:00:01+00:00 |
| 2 | A | 2 | success | 200 | ok | 2026-10-04T00:00:02+00:00 |
| 3 | B | 7 | **failure** | 500 | http_status | 2026-10-04T00:00:03+00:00 |
| 4 | A | 3 | **failure** | null | timeout | 2026-10-04T00:00:04+00:00 |
| 5 | A | 9 | success | 204 | ok | 2026-10-04T00:00:05+00:00 |

即 id 1..5 的目标为 A、A、B、A、A；状态为 failure、success、failure、
failure、success；耗时为 0、2、7、3、9。三条 failure 的 `reason` 刻意取了
三种不同值，以证明筛选**只看 status、不区分失败原因**。

## 7. 验收示例三：`--url` 与 `--status` 组合（普通查询 + 摘要）

```sh
python healthcheck.py --db monitor.sqlite recent \
    --url http://127.0.0.1:8765/health?detail=1 --status failure --limit 2
```

实际输出（退出码 `0`，stderr 为空）：

```json
[{"id":4,"url":"http://127.0.0.1:8765/health?detail=1","checked_at":"2026-10-04T00:00:04.000000+00:00","elapsed_ms":3,"status":"failure","http_status":null,"reason":"timeout"},{"id":1,"url":"http://127.0.0.1:8765/health?detail=1","checked_at":"2026-10-04T00:00:01.000000+00:00","elapsed_ms":0,"status":"failure","http_status":null,"reason":"connection_error"}]
```

要点：

- 命中条件是 **url 等于 A 且 status 等于 failure**：
  `WHERE url = ? AND status = ?`（由 `build_recent_query` 按组合组装，
  `healthcheck.py:461-485`，执行点 `561-576`）；
- A 的 failure 只有 id 1、4，倒序取前 2 得 **4、1**；
  A 的两条 success（id 2、5）被 status 条件排除；
  B 的 id 3 虽是 failure，但 url 不同，被 url 条件排除；
- **先筛选后限量**：`LIMIT 2` 作用在两个条件的交集之上（交集本来就只有 2 条）。

同一命令加 `--summary`：

```sh
python healthcheck.py --db monitor.sqlite recent \
    --url http://127.0.0.1:8765/health?detail=1 --status failure \
    --limit 2 --summary
```

```json
{"count":2,"min_elapsed_ms":0,"max_elapsed_ms":3,"avg_elapsed_ms":1.5}
```

统计口径与第 5 节完全相同：只统计这次普通查询本会返回的 id 4、1 两条；
id 1 的零耗时参与统计（min 为 0，count 仍为 2）；均值 `(3 + 0) / 2 = 1.5`
不取整。

补充：只给 `--status failure`（省略 `--url`，默认 limit 5）时跨全部目标命中
id 4、3、1（B 的 id 3 也在内），对应 `WHERE status = ?` 组合；
`--status success` 则命中 id 5、2。

---

## 8. 固定样本三（`test_recent_reason_filter.py`，5 条 reason 验收样本）

该样本专门验收 `--reason`，共 5 条记录，A 与 B **仅路径不同**：

- A：`http://127.0.0.1:8765/health`
- B：`http://127.0.0.1:8765/ready`

| id | 目标 | elapsed_ms | status | http_status | reason | checked_at (UTC) |
|----|------|-----------|--------|-------------|--------|------------------|
| 1 | A | **0** | **failure** | null | timeout | 2026-10-04T00:00:01+00:00 |
| 2 | A | 4 | success | 200 | ok | 2026-10-04T00:00:02+00:00 |
| 3 | A | **10** | **failure** | null | timeout | 2026-10-04T00:00:03+00:00 |
| 4 | B | 20 | **failure** | null | timeout | 2026-10-04T00:00:04+00:00 |
| 5 | A | 9 | success | 200 | ok | 2026-10-04T00:00:05+00:00 |

即 id 1..5 依次保存 A 的 timeout、A 的 ok、A 的 timeout、B 的 timeout、
A 的 ok；耗时依次为 0、4、10、20、9 毫秒；ok 对应 success，其余对应
failure。三条 timeout 分属不同目标与耗时，以证明 reason 精确匹配与
url/status 交集都按保存值生效。

## 9. 验收示例四：`--url` 与 `--reason` 组合（普通查询 + 摘要）

```sh
python healthcheck.py --db monitor.sqlite recent \
    --url http://127.0.0.1:8765/health --reason timeout --limit 2
```

实际输出（退出码 `0`，stderr 为空）：

```json
[{"id":3,"url":"http://127.0.0.1:8765/health","checked_at":"2026-10-04T00:00:03.000000+00:00","elapsed_ms":10,"status":"failure","http_status":null,"reason":"timeout"},{"id":1,"url":"http://127.0.0.1:8765/health","checked_at":"2026-10-04T00:00:01.000000+00:00","elapsed_ms":0,"status":"failure","http_status":null,"reason":"timeout"}]
```

要点：

- 命中条件是 **url 等于 A 且保存的 reason 等于 timeout**：
  `WHERE url = ? AND reason = ?`（由 `build_recent_query` 按组合组装，
  `healthcheck.py:461-485`，执行点 `561-576`）；
- A 的 timeout 只有 id 1、3，倒序取前 2 得 **3、1**；
  A 的两条 ok（id 2、5）被 reason 条件排除；
  B 的 id 4 虽也是 timeout，但 url 不同，被 url 条件排除；
- **先筛选后限量**：`LIMIT 2` 作用在两个条件的交集之上；
- reason 条件只比较保存的 reason 列，failure 状态与 null 的 http_status
  都不参与推断。

同一命令加 `--summary`：

```sh
python healthcheck.py --db monitor.sqlite recent \
    --url http://127.0.0.1:8765/health --reason timeout \
    --limit 2 --summary
```

```json
{"count":2,"min_elapsed_ms":0,"max_elapsed_ms":10,"avg_elapsed_ms":5.0}
```

统计口径与第 5 节完全相同：只统计这次普通查询本会返回的 id 3、1 两条；
id 1 的零耗时参与统计（min 为 0，count 仍为 2）；均值 `(10 + 0) / 2 = 5.0`
不取整（JSON 中保留为 `5.0`）。

补充组合（默认 limit 5，样本均未被截断）：

- 只给 `--reason timeout`（省略 `--url`、`--status`）跨全部目标命中
  id 4、3、1，对应 `WHERE reason = ?` 组合；`--reason ok` 命中 id 5、2；
  `--reason http_status` 与 `--reason connection_error` 在本样本均为 `[]`
  （尽管存在 failure 记录——证明不从 status/http_status 推断）。
- `--url A --status failure --reason timeout` 三条件交集仍命中 id 3、1，
  对应 `WHERE url = ? AND status = ? AND reason = ?` 组合；
  `--status success --reason timeout` 交集为空，
  普通模式输出 `[]`、摘要输出 count 0 三 null，退出码均为 0。

---

## 10. 固定样本四（`test_recent_since.py`，4 条）

该样本专门验收 `--since`，共 4 条记录，**全部 failure / timeout、
`http_status` 为 null**，A 与 B 仅路径不同：

- 目标 A：`http://127.0.0.1:8765/health`
- 目标 B：`http://127.0.0.1:8765/ready`

| id | 目标 | elapsed_ms | status | http_status | reason | checked_at（原始字符串） |
|----|------|-----------|--------|-------------|--------|--------------------------|
| 1 | A | 0 | failure | null | timeout | `2026-10-04T00:00:02Z` |
| 2 | A | 10 | failure | null | timeout | `2026-10-04T00:00:02.000000+00:00` |
| 3 | B | 20 | failure | null | timeout | `2026-10-04T00:00:04Z` |
| 4 | A | 30 | failure | null | timeout | `2026-10-04T00:00:01Z` |

样本的刻意安排：

- **id 1 与 id 2 属于 A、是同一时刻（00:00:02）的两种字符串写法**
  （`Z` 省略小数 vs `+00:00` 带六位零小数），用于区分「按时刻比较」与
  「按字符串比较」：若按字符串比较，两者不会同时命中；
- id 4 属于 A 但早一秒（00:00:01），用于验证起点之后才被保留；
- id 3 属于 B 且更晚（00:00:04），用于验证 url 条件先于时间条件生效；
- 耗时 0、10、20、30 毫秒，用于核对摘要只统计最终返回的记录。

## 11. 验收示例五：`--since` 时间筛选（普通查询 + 摘要）

以下输出为**按第 10 节固定样本推定的预期结果**（退出码 `0`，stderr 为空），
可由 `test_recent_since.py` 的公开命令行用例核对。

普通查询——目标 A、`--status failure`、`--reason timeout`、起点
`2026-10-04T00:00:02Z`、`--limit 2`：

```sh
python healthcheck.py --db monitor.sqlite recent \
    --url http://127.0.0.1:8765/health --status failure --reason timeout \
    --since 2026-10-04T00:00:02Z --limit 2
```

预期输出（单行 JSON）：

```json
[{"id":2,"url":"http://127.0.0.1:8765/health","checked_at":"2026-10-04T00:00:02.000000+00:00","elapsed_ms":10,"status":"failure","http_status":null,"reason":"timeout"},{"id":1,"url":"http://127.0.0.1:8765/health","checked_at":"2026-10-04T00:00:02Z","elapsed_ms":0,"status":"failure","http_status":null,"reason":"timeout"}]
```

要点：

- **起点相等为何命中**：保留规则是「不早于起点」，`filter_rows_by_since`
  中的比较是 `checked_at >= since`（`healthcheck.py:295`），id 1、2 与起点
  同一时刻，因此保留。
- **`Z` 与 `+00:00`、显式零小数与省略小数为何等价**：`parse_utc_timestamp`
  （`healthcheck.py:204-225`）把两种时区结尾都解析为
  `tzinfo=timezone.utc` 的 aware `datetime`，缺省小数按 `"0"` 右补零到微秒
  （`healthcheck.py:216`），所以 id 1 的 `2026-10-04T00:00:02Z` 与 id 2 的
  `2026-10-04T00:00:02.000000+00:00` 解析为**同一时刻**，比较按时刻而非
  字符串进行。同理，起点写成 `--since 2026-10-04T00:00:02.000000+00:00`
  结果完全相同。
- **输出为何保留原始 `checked_at`**：解析出的 `datetime` 只用于比较，
  输出仍取行中保存的原始字符串（记录映射 `healthcheck.py:609-620`），
  不统一改写成 `Z` 或 `+00:00`——所以 id 2 输出 `...02.000000+00:00`、
  id 1 输出 `...02Z`，原样不同。
- **筛选完成后才限量**：`--since` 生效时 SQL 不加 `LIMIT`
  （`build_recent_query(..., sql_limit=False)`，调用点 `healthcheck.py:561-569`），
  先取出符合 url/status/reason 的全部记录，时间过滤后才 `kept[:limit]`
  截取（`healthcheck.py:297`）。
- **排序依据 id**：顺序来自 SQL 的 `ORDER BY id DESC`
  （`SELECT_SQL`，`healthcheck.py:106-112`），时间筛选不改变相对顺序，
  因此 id 2 排在 id 1 之前；`checked_at` 同样不参与排序。
- **其余两条记录的去向**：id 4（00:00:01）早于起点，被时间条件排除；
  id 3 属于目标 B，在 SQL 阶段即被 `WHERE url = ?` 排除，根本不进入
  时间校验。

同一命令加 `--summary`：

```sh
python healthcheck.py --db monitor.sqlite recent \
    --url http://127.0.0.1:8765/health --status failure --reason timeout \
    --since 2026-10-04T00:00:02Z --limit 2 --summary
```

预期输出（单行 JSON）：

```json
{"count":2,"min_elapsed_ms":0,"max_elapsed_ms":10,"avg_elapsed_ms":5.0}
```

**摘要只统计最终返回的记录**：口径与第 5 节相同，统计的就是上面普通查询
返回的 id 2、1 两条（耗时 10、0 毫秒）——`count` 为 2，最小 0、最大 10，
平均 `(10 + 0) / 2 = 5.0` 不取整；id 4 的 30 毫秒被时间条件排除、id 3 的
20 毫秒被 url 条件排除，均不计入。

补充（同样本、同筛选，可由 `test_recent_since.py` 核对）：

- 起点推进一微秒（`--since 2026-10-04T00:00:02.000001Z`）后 id 1、2 都早于
  起点：普通查询输出 `[]`，摘要输出
  `{"count":0,"min_elapsed_ms":null,"max_elapsed_ms":null,"avg_elapsed_ms":null}`，
  退出码均为 `0`——**无匹配不是错误**。
- **省略 `--since`**（其余条件相同）：不增加时间条件、也不校验
  `checked_at`，id 4（00:00:01）照常入选，按 id 倒序取前 2 得 **4、2**；
  即使库中存在格式非法的 `checked_at` 记录也照常返回，不做时间校验。

---

## 12. 空结果与错误的区别

两种情况退出码、输出通道完全不同。

### 12.1 空结果：退出码 0，正常输出

| 情形 | 普通查询 stdout | 摘要 stdout | 代码位置 |
|---|---|---|---|
| 数据库文件不存在（父目录也不存在） | `[]` | `{"count":0,"min_elapsed_ms":null,"max_elapsed_ms":null,"avg_elapsed_ms":null}` | `healthcheck.py:514-519` |
| 有效数据库但没有历史表（含仅有其他表） | `[]` | 同上（count 0、三个 null） | `548-550` + `599-605` |
| 历史表存在但为空表 | `[]` | 同上 | 查询返回 0 行，`584-607` |
| 合法条件但无匹配记录（URL/status/reason/since 任一组合无命中，含起点晚于全部记录） | `[]` | 同上 | 对应 SELECT 返回 0 行或时间过滤后为空 |

这些情形 stderr 均为空，且不创建文件/目录、不补建历史表，其他表与数据
保持不变。历史表名按大小写不敏感识别：`CHECKS`、`Checks` 等与 `checks`
得到完全相同的查询结果（回归测试 `test_recent_case_table.py`、
`test_recent_reason_filter.py::test_reason_filter_works_with_case_variant_table`）。

### 12.2 错误：退出码 2，stdout 为空，原因写入 stderr

| 情形 | 报告位置 / 实测信息要点 |
|---|---|
| 非法 `--status`（非 `success`/`failure`、大小写不符、空字符串、带空白） | `validate_status_filter` → `die`，前缀 `healthcheck: error:`，信息含「status 参数错误」并回显实际值（`healthcheck.py:156-175, 494`） |
| 裸 `--status` 缺少值 | 哨兵 `STATUS_FILTER_MISSING` 被同一校验拒绝，信息说明「--status 必须提供值」（`healthcheck.py:165-169, 652-653`） |
| 非法 `--reason`（非四个固定值、大小写不符、空字符串、前后空白） | `validate_reason_filter` → `die`，前缀 `healthcheck: error:`，信息含「reason 参数错误」并回显实际值（`healthcheck.py:178-201, 505`） |
| 裸 `--reason` 缺少值 | 哨兵 `REASON_FILTER_MISSING` 被同一校验拒绝，信息说明「--reason 必须提供值……实际缺少值」（`healthcheck.py:189-194, 662-663`） |
| 非法 `--since`（空值、前后空白、缺时区、非 UTC 偏移如 `+08:00`、非法日期时间如 `2026-02-30`、其他格式不符如七位小数） | `validate_since_filter` → `die`，前缀 `healthcheck: error:`，信息含「since 参数错误」与具体原因（来自 `since_format_problem`）并回显实际值（`healthcheck.py:253-273, 228-250, 510`） |
| 裸 `--since` 缺少值 | 哨兵 `SINCE_FILTER_MISSING` 被同一校验拒绝，信息说明「--since 必须提供值……实际缺少值」（`healthcheck.py:264-269, 673-674`） |
| `--since` 生效时，符合其余筛选的记录 `checked_at` 格式非法 | `filter_rows_by_since` → `die`，信息指出记录 id（如 `记录 id=1 的 checked_at 不符合 UTC 时间格式...`）并回显原值（`healthcheck.py:276-297`，调用点 `574-576`）；**校验在 limit 截取之前针对全部符合其余筛选的记录**，坏记录即使按 id 倒序排在 limit 之外也照样退出 2；被 url/status/reason 排除的坏记录不参与校验、不影响查询；省略 `--since` 时不做此校验 |
| 非法 `--url`（非 http、非 127.0.0.1、缺端口、端口越界、userinfo、含 `#`、空白或结构非法等） | `validate_target_url` → `die`，前缀 `healthcheck: error:`，并回显非法输入（`healthcheck.py:300-350, 499-500`） |
| 非法 `--limit`（0、负数、`1.5`、非数字） | argparse 在进入 `command_recent` **之前**拒绝，退出码 2、stdout 空、用法与原因写 stderr（`positive_limit`，`healthcheck.py:145-153`） |
| `--db` 路径是目录 | `healthcheck.py:522-523`，信息含「目录」 |
| 历史表（含 `CHECKS`/`Checks` 等异写）缺少任一所需字段 | 查询前由 `ensure_checks_columns` 拒绝，信息为「checks 表缺少字段: …」并列出缺失字段（`healthcheck.py:554, 372-384`）；不再返回空历史 |
| 文件存在但不是有效 SQLite 数据库 | 查询期错误，实测为 `file is not a database`（`healthcheck.py:579-582`） |
| 读取权限不足 | 只读打开/查询失败，实测为 `unable to open database file`（`healthcheck.py:527-531, 579-582`） |

优先级：argparse 先解析 `--limit`，故 limit 非法时根本不会进入处理函数；
进入 `command_recent` 后**依次校验 status、URL、reason、since，最后才访问
数据库**，因此报告顺序为：
**status 错误 ＞ URL 错误 ＞ reason 错误 ＞ since 错误 ＞ 数据库路径错误**
（例如非法 status 配合非法 URL、非法 reason、非法 since 与目录路径，只报
status 错误；status、URL、reason 都合法后非法 since 仍先于目录错误，且此时
数据库尚未被访问）。所有 `die` 错误都只有 stderr、无 Python 回溯
（`die`，`healthcheck.py:129-132`）。

---

## 13. 只读、无副作用保证

- **不发送网络请求**：`recent` 路径不调用 `probe_once`（`healthcheck.py:387-411`），
  URL 仅做本地合法性校验与字符串匹配。回归用例
  `test_summary_makes_no_network_requests` 与
  `test_recent_reason_filter.py::test_reason_query_makes_no_network_requests`
  在本地起服务器核对请求数为 0。
- **不创建文件或目录**：数据库/父目录缺失时直接输出空结果
  （`healthcheck.py:514-519`），不调用会建库的 `open_database`；
  `test_recent_status_filter.py` 与 `test_recent_reason_filter.py` 用刻意
  不存在的数据库路径核对：非法 `--status`/`--reason` 被拒绝时也不会落库
  或建目录；`test_recent_since.py` 同样核对非法 `--since` 被拒绝时不落库、
  不建目录。
- **不创建表、不改动记录**：库以 `?mode=ro` 打开（`healthcheck.py:527`），
  全程只有 `SELECT`（含对 `sqlite_master` 的查询）；无历史表时仅置空结果。
  `--since` 生效时对坏 `checked_at` 的拒绝同样只读不写。
  测试通过前后目录文件集合与全表内容快照比对确认无变化。
- **可读但不可写的数据库仍可查询**：只读 URI 不需要写权限，也不生成
  `-wal`/`-journal` 旁路文件（`test_readonly_database_is_queryable` 覆盖了
  带 `--status`、`--reason` 组合条件的只读查询）。

## 14. 保持不变的既有语义

- 默认 `--limit 5`（`DEFAULT_LIMIT`）；未给 `--url`、`--status`、`--reason`、
  `--since` 时跨全部目标按 id 倒序返回（`SELECT_SQL`）。
- **省略 `--status`、`--reason`、`--since` 的查询输出与新增这些参数前完全
  一致**：普通查询仍返回成功与失败混合、各原因混合的记录数组，摘要仍对同一
  批记录统计（样本见 `test_omitting_status_keeps_legacy_behavior` 与
  `test_omitting_reason_keeps_legacy_behavior`）；省略 `--since` 时不增加
  时间条件，也不校验记录的 `checked_at`（格式非法的时间字符串照常原样
  返回）。
- `check` 子命令不接受 `--status`、`--reason`、`--since`；其探测与写入行为
  不变：`probe_once` 只发一次 GET、不跟随重定向，成功/失败经 `INSERT_SQL`
  落库后才输出（`command_check`，`healthcheck.py:418-458`）；建库建表仍只由
  `check` 的 `open_database` 完成。reason 的取值仍只由探测结果决定
  （2xx→ok、非 2xx→http_status、超时→timeout、其他连接错误→
  connection_error），recent 的 `--reason` 只读这些已保存的值。
- 记录七字段、表结构（`CREATE_TABLE_SQL`，`healthcheck.py:76-87`）与退出码
  约定（成功 0 / 已记录的探测失败 1 / 参数或数据库错误 2）均不变。

## 15. 结论到源码位置的对照

| 结论 | 函数 / 常量 | 位置 |
|---|---|---|
| recent 处理总流程 | `command_recent` | `healthcheck.py:488-622` |
| status 必须为区分大小写的 success/failure，且最先校验 | `validate_status_filter` / 哨兵 `STATUS_FILTER_MISSING` | `156-175` / `41`，调用点 `494` |
| URL 合法性规则（先于 reason/since/DB 路径、晚于 status 执行） | `validate_target_url` | `300-350`，调用点 `499-500` |
| reason 必须为区分大小写的四个固定值；status、URL 之后、since 与 DB 之前校验 | `validate_reason_filter` / 哨兵 `REASON_FILTER_MISSING` / `REASON_FILTER_CHOICES` | `178-201` / `48` / `52-57`，调用点 `505` |
| since 必须为严格 UTC 格式且日期时间真实存在；status、URL、reason 之后、DB 之前校验 | `validate_since_filter` / `since_format_problem` / 哨兵 `SINCE_FILTER_MISSING` | `253-273` / `228-250` / `61`，调用点 `510` |
| since 与 checked_at 共用的严格 UTC 格式（Z 或 +00:00 结尾、一至六位小数） | `UTC_TIMESTAMP_RE` / `UTC_TIMESTAMP_BODY_RE` | `66-68` / `72-74` |
| 时刻解析：Z ≡ +00:00、省略零小数 ≡ 显式零小数 | `parse_utc_timestamp` | `204-225` |
| 时间筛选后处理：校验全部符合其余筛选的记录（坏记录指出 id、在 limit 之前）、`>=` 起点保留、过滤后才限量 | `filter_rows_by_since` | `276-297`，调用点 `574-576` |
| `--since` 生效时 SQL 不加 LIMIT（时间条件在 Python 侧按时刻比较） | `build_recent_query` 的 `sql_limit` 参数 | `461-485`，调用点 `561-569` |
| limit 必须为正整数、默认 5 | `positive_limit` / `DEFAULT_LIMIT` | `145-153` / `29` |
| 缺库（含父目录缺失）视为空，不创建 | 路径存在性分支 | `514-519` |
| 目录路径报错（退出码 2） | 目录分支 + `die` | `522-523` |
| 只读打开、不可写也能查 | `?mode=ro` URI | `527-531` |
| 历史表名大小写不敏感识别（CHECKS/Checks 同 checks） | `sqlite_master` 判断 | `536-547` |
| 无历史表视为空、不建表 | 表缺失分支 | `548-550` |
| 历史表缺字段以退出码 2 报缺列（不误判为空历史） | `ensure_checks_columns` | `372-384`，调用点 `554` |
| 唯一查询模板：按 id 倒序，WHERE 由组合生成，LIMIT 可选 | `SELECT_SQL` / `RECENT_FILTER_CLAUSES` | `106-112` / `116-120` |
| 八种筛选组合统一组装：条件 AND，先筛选后限量 | `build_recent_query` | `461-485`，调用点 `561-569` |
| URL 条件：原始字符串精确匹配 | `url = ?` 条件片段 | `116-120` |
| status 条件：按记录 status 等值匹配（不看 reason） | `status = ?` 条件片段 | `116-120` |
| reason 条件：按保存的 reason 精确匹配（不看 status/http_status） | `reason = ?` 条件片段 | `116-120` |
| 摘要统计同条件记录子集；失败与零耗时计入；均值不取整 | 摘要分支 | `584-607` |
| 空摘要固定输出（count 0、三个 null） | `NULL_SUMMARY_JSON` | `123-126` |
| 完整七字段记录数组 / `[]` 输出（checked_at 保留原始字符串） | 记录映射与 JSON 输出 | `609-621` |
| 错误统一出口（退出码 2、stderr、stdout 空） | `die` | `129-132` |
| 网络探测与建库/写入仅属于 check（recent 不触碰） | `probe_once` / `open_database` / `INSERT_SQL` | `387-411` / `353-369` / `89-92` |
