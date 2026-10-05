# `recent --status-summary` 状态摘要流程说明

本说明描述 `healthcheck.py` **当前已实现**的 `recent --status-summary` 从命令行
输入到状态计数输出的完整行为，不新增、不修改任何行为：程序、数据库结构、
现有测试与公开入口保持不变。内容沿「互斥检查 → 筛选值校验 → 数据库只读查询
→（时间边界生效时）Python 侧时刻过滤与限量 → 状态计数 → 单行 JSON 输出」
讲清每一步，关键结论均标注对应的源码函数或位置（行号以当前 `healthcheck.py`
为准）。`recent` 普通查询与五个可选筛选（`--url`/`--status`/`--reason`/
`--since`/`--until`）的完整规则见 `RECENT_QUERY_FLOW.md`，本文只在其之上
说明状态摘要特有的部分。

- 入口函数：`main`（`healthcheck.py:809-812`）→ `command_recent`
  （`healthcheck.py:588-712`）
- 状态计数构造：`build_status_summary`（`healthcheck.py:554-566`）
- 三种输出形态的统一呈现入口：`render_recent_result`
  （`healthcheck.py:569-585`），`--status-summary` 分支在
  `healthcheck.py:581-582`
- `recent` **不发送网络请求、不创建文件/目录/表、不改动已有记录**；数据库以
  只读 URI 打开，可读但不可写的库仍可查询（见第 7 节）。

> 文中所有命令输出块均为**源码推导的预期结果**（依据当前实现逐条核对，核对
> 方式见 `test_recent_status_summary_stored_status.py` 与
> `test_recent_status_summary.py`），表示在第 4 节样本库上运行时应当得到的
> stdout / 退出码 / stderr；它们不是「本文档生成时已实际运行」的实测记录。
> 你可照第 4.2 节准备样本库并按第 5 节命令自行复现。

---

## 1. 统计口径：只统计普通 `recent` 会返回的那批记录

`--status-summary` **不改变记录的选择过程**，只改变最后的呈现形式。它统计的
是**同条件普通 `recent`（不带 `--status-summary`）会返回的同一批记录**——
同样的筛选、同样的排序、同样的 limit 截取。具体顺序（实现见
`command_recent`，`healthcheck.py:588-712`）：

1. **先取各筛选条件的交集**：`--url`、`--status`、`--reason` 三个库内筛选
   由 `build_recent_query`（`healthcheck.py:489-513`，调用点
   `healthcheck.py:683-691`）统一组装为一条 SQL：模板 `SELECT_SQL`
   （`healthcheck.py:110-116`）加 `RECENT_FILTER_CLAUSES`
   （`healthcheck.py:120-124`）中对应的条件片段（`url = ?` / `status = ?` /
   `reason = ?`），条件之间一律 **AND**，无筛选时无 WHERE 子句。
2. **再按 id 倒序**：所有组合一律 `ORDER BY id DESC`（`SELECT_SQL`，
   `healthcheck.py:114`），排序键只有 id，与 `checked_at` 无关。
3. **再限量**：`--limit` 为正整数，默认 **5**（`DEFAULT_LIMIT`，
   `healthcheck.py:32`；校验见 `positive_limit`，`healthcheck.py:142-150`）。
   无时间边界时 SQL 直接带 `LIMIT ?`，库内一次完成「筛选 → 倒序 → 取前
   limit 条」（`healthcheck.py:692-693`）。
4. **有时间边界时先完成时间窗口筛选，再限量**：`--since` / `--until` 任一
   生效时（`time_filter_active`，`healthcheck.py:634`），SQL **不加 LIMIT**
   （`build_recent_query(..., sql_limit=False)`），先取出符合其余筛选的全部
   记录，再由 `filter_rows_by_since`（`healthcheck.py:279-306`，调用点
   `healthcheck.py:696-698`）在 Python 侧按时刻保留窗口内记录
   （`since <= checked_at <= until`，两个端点都包含，省略的一侧不设限），
   **最后才** `kept[:limit]` 截取（`healthcheck.py:306`）。

无论是否给出时间边界，语义都统一为**先筛选、后限量**：limit 永远作用在筛选
（含时间窗口）之后，不会先截断再筛选。状态摘要对截断后的同一 `rows` 计数，
并不另外发起任何查询（`render_recent_result` 把同一批 `rows` 交给
`build_status_summary`，`healthcheck.py:581-582`）。

因此状态摘要**不是全历史统计**：被 limit 截掉的记录、被 url/status/reason
排除的记录、被时间窗口排除的记录都不计入。

## 2. 输出格式：单行 JSON 对象，恰含三个整数字段

输出为**一行紧凑 JSON 对象**加一个换行（`json.dumps(..., separators=(",",
":"))`，`render_recent_result`，`healthcheck.py:585`），恰含三个字段，
均为整数：

| 字段 | 含义 | 代码位置 |
|---|---|---|
| `count` | 该批记录的总数（`len(rows)`） | `healthcheck.py:559` |
| `success_count` | 其中保存的 `status` 等于 `success` 的数量 | `healthcheck.py:560-562` |
| `failure_count` | 其中保存的 `status` 等于 `failure` 的数量 | `healthcheck.py:563-565` |

成功时退出码为 `0`，stderr 为空。

## 3. 分类依据：只看保存的 `status`，不从 `reason` 或 `http_status` 重新判断

`build_status_summary`（`healthcheck.py:554-566`）的分类条件是
`row[4] == STATUS_SUCCESS` / `row[4] == STATUS_FAILURE`（`STATUS_SUCCESS` /
`STATUS_FAILURE` 定义于 `healthcheck.py:39-40`），即**只比较数据库保存的
`status` 字段**：

- **绝不根据 `reason` 重新判断**：保存为 `success` 但 `reason` 为 `timeout`
  的记录仍计入 `success_count`，不会因「超时通常意味着失败」而改判；
- **绝不根据 `http_status` 重新判断**：保存为 `success` 但 `http_status`
  为 500 的记录仍计入 `success_count`；保存为 `failure` 但 `http_status`
  为 200 的记录仍计入 `failure_count`。

表约束（`CREATE_TABLE_SQL`，`healthcheck.py:80-91`）保证 `status` 只可能是
`success` 或 `failure`，故 `count` 恒等于 `success_count + failure_count`。

同理，筛选条件本身也只看保存值：`--reason` 按保存的 `reason` 精确匹配
（`reason = ?`，不从 `status`/`http_status` 推断）；`--url` 沿用命令行
**原始字符串精确匹配**（`url = ?`，校验只判定输入合法性，匹配不做任何
规范化）；时间窗口沿用现有 **UTC 时刻比较**与**端点包含**规则（`Z` 与
`+00:00`、显式零小数与省略小数视为同一时刻；`>= since` 且 `<= until`）。
这些规则与普通 `recent` 完全一致，详见 `RECENT_QUERY_FLOW.md` 第 1、6 节。

## 4. 固定样本与本地 SQLite 准备

### 4.1 固定样本（复用 `test_recent_status_summary_stored_status.py`，3 条）

该样本专门验收「只按保存的 status 计数」：三条记录共用同一目标
`http://127.0.0.1:8765/health`、同一 `checked_at`
`2026-10-05T00:00:00Z`、零耗时；**`status` 与 `reason`/`http_status` 的组合
在含义上刻意相互矛盾**，但每一行都满足 `checks` 表的全部约束：

| id | url | checked_at | elapsed_ms | status | http_status | reason |
|----|-----|-----------|-----------|--------|-------------|--------|
| 1 | `http://127.0.0.1:8765/health` | `2026-10-05T00:00:00Z` | 0 | **success** | null | **timeout** |
| 2 | `http://127.0.0.1:8765/health` | `2026-10-05T00:00:00Z` | 0 | **success** | **500** | http_status |
| 3 | `http://127.0.0.1:8765/health` | `2026-10-05T00:00:00Z` | 0 | **failure** | **200** | **ok** |

关键安排：若按 `reason` 或 `http_status` 推断，会得到「1 成功 2 失败」
（id 1 超时、id 2 是 500、id 3 是 200/ok）；只有按保存的 `status` 计数才是
「2 成功 1 失败」。样本与测试文件中的 `REC1`/`REC2`/`REC3`
（`ALL_RECORDS`）逐字段一致。

### 4.2 可复现的本地样本准备

只用 Python 3 标准库，直接建库，**不发任何网络请求**；与测试夹具
`build_sample_db` 写入的内容等价。在项目目录执行：

```sh
python3 - <<'EOF'
import sqlite3
conn = sqlite3.connect("monitor.sqlite")
conn.execute("""
CREATE TABLE checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    elapsed_ms INTEGER NOT NULL CHECK (elapsed_ms >= 0),
    status TEXT NOT NULL CHECK (status IN ('success', 'failure')),
    http_status INTEGER,
    reason TEXT NOT NULL CHECK (reason IN
        ('ok', 'http_status', 'connection_error', 'timeout'))
)
""")
conn.executemany(
    "INSERT INTO checks (id, url, checked_at, elapsed_ms, status, "
    "http_status, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
    [
        (1, "http://127.0.0.1:8765/health", "2026-10-05T00:00:00Z",
         0, "success", None, "timeout"),
        (2, "http://127.0.0.1:8765/health", "2026-10-05T00:00:00Z",
         0, "success", 500, "http_status"),
        (3, "http://127.0.0.1:8765/health", "2026-10-05T00:00:00Z",
         0, "failure", 200, "ok"),
    ],
)
conn.commit()
conn.close()
EOF
```

表结构即产品代码的 `CREATE_TABLE_SQL`（`healthcheck.py:80-91`），测试文件
`test_recent_status_summary_stored_status.py` 中的 `SCHEMA_SQL` 与其逐字
一致。准备完成后即可照第 5 节命令复现。**第 5 节给出的输出均为该样本上的
预期结果**（逐条对应测试：`test_counts_use_stored_status_only`、
`test_reason_timeout_filter_matches_success_record`），不是某次实际运行的
转存。

## 5. 验收示例（预期结果，源码推导）

### 5.1 省略全部筛选：总数 3、成功 2、失败 1

```sh
python healthcheck.py --db monitor.sqlite recent --status-summary
```

预期输出（单行 JSON；退出码 `0`，stderr 为空）：

```json
{"count":3,"success_count":2,"failure_count":1}
```

要点：

- 无筛选条件，SQL 无 WHERE 子句，默认 `--limit 5` 不截断（样本只有 3 条），
  三条记录全部计入；
- id 1（success 但 reason 为 timeout）与 id 2（success 但 http_status 为
  500）都计入 `success_count`；id 3（failure 但 http_status 为 200、reason
  为 ok）计入 `failure_count`——分类只依据保存的 `status`（见第 3 节）。

### 5.2 追加 `--reason timeout`：总数 1、成功 1、失败 0

```sh
python healthcheck.py --db monitor.sqlite recent --status-summary --reason timeout
```

预期输出（单行 JSON；退出码 `0`，stderr 为空）：

```json
{"count":1,"success_count":1,"failure_count":0}
```

要点：

- `--reason timeout` 按保存的 `reason` 精确匹配，三条样本中只有 id 1 命中
  （`WHERE reason = ?`）；
- id 1 保存的 `status` 是 `success`：即使 `timeout` 通常意味着失败也**不得
  改判**，故 `success_count` 为 1、`failure_count` 为 0。

补充（同样为源码推导的预期值，对应测试
`test_limit_2_counts_latest_two_by_id_desc`）：同一命令加 `--limit 2`
（省略 `--reason`）时，先按 id 倒序截取最新两条 id 3（failure）、
id 2（success），预期输出
`{"count":2,"success_count":1,"failure_count":1}`——证明 **limit 截取发生
在计数之前**。

## 6. 空结果与互斥错误

### 6.1 无匹配、缺库、无历史表、空表：三个计数均为零，退出码 0

以下情形**都不是错误**，退出码均为 `0`、stderr 为空，输出同为
`{"count":0,"success_count":0,"failure_count":0}`（空记录集经同一呈现入口
`render_recent_result` 由 `build_status_summary` 构造，`healthcheck.py:556`
的文档字符串明确「空集三个计数均为 0」）：

| 情形 | 代码位置 |
|---|---|
| 数据库文件不存在（父目录也不存在也一样；不创建任何东西） | 缺库快路径以空记录集直接走呈现入口，`healthcheck.py:639-641` |
| 有效数据库但没有历史表（空库或仅有其他表） | 表缺失分支置 `rows = []`，不建表，`healthcheck.py:670-672` |
| 历史表存在但为空表 | 查询返回 0 行 |
| 合法筛选条件但无匹配记录（含时间窗口过滤后无保留） | 对应 SELECT 返回 0 行 / `filter_rows_by_since` 返回空 |

历史表名按大小写不敏感识别（`CHECKS`、`Checks` 等与 `checks` 是同一历史表，
`healthcheck.py:664-669`）；表存在但缺任一所需字段时不是空结果，而是由
`ensure_checks_columns`（`healthcheck.py:381-393`，调用点
`healthcheck.py:676`）以退出码 `2` 报缺列。

### 6.2 `--summary` 与 `--status-summary` 互斥：退出码 2

同时指定 `--summary` 与 `--status-summary` 时：

- 退出码 `2`，**stdout 为空**，stderr 指出两者互斥：
  `healthcheck: error: 参数错误：--summary 与 --status-summary 互斥，一次查询只能选用其中一种摘要`；
- 该检查是 `command_recent` 的**第一步**（`healthcheck.py:591-597`），发生
  在 status/URL/reason/since/until 等**筛选值校验和一切数据库访问之前**——
  即使同时给出非法筛选值或不存在的数据库路径，也只报互斥错误，且不会读取
  数据库（对应测试
  `test_summary_and_status_summary_are_mutually_exclusive`，该用例同时确认
  库与目录不变）。

其余参数错误（非法 `--status`/`--reason`/`--since`/`--until`/`--url`/
`--limit`、起点晚于终点、目录路径、缺字段、无效库文件等）的退出码、通道与
报告顺序与普通 `recent` 完全一致，见 `RECENT_QUERY_FLOW.md` 第 9、10 节；
错误统一出口为 `die`（`healthcheck.py:126-129`：写 stderr、退出码 2、
stdout 保持为空、无 Python 回溯）。

## 7. 只读、无副作用保证

- **不发送网络请求**：`recent` 路径不调用 `probe_once`
  （`healthcheck.py:413-437`），URL 仅做本地合法性校验与字符串匹配。
- **不创建文件或目录**：数据库/父目录缺失时直接输出三个零的计数
  （`healthcheck.py:639-641`），不调用会建库建表的 `open_database`。
- **不创建表、不改动已有记录**：库以只读 URI（`?mode=ro`）打开
  （`healthcheck.py:649-653`），全程只有 `SELECT`（含对 `sqlite_master`
  的查询）；无历史表时仅置空结果，不补建表。
- **可读但不可写的数据库仍可查询**：只读 URI 不需要写权限，也不产生
  `-wal`/`-journal` 旁路文件。
- 回归测试 `test_recent_status_summary_stored_status.py` 的每个用例都用
  `snapshot_state` 逐一比对查询前后的目录文件集合、表结构与全部记录内容，
  确认状态摘要查询严格只读。

## 8. 结论到源码位置的对照

| 结论 | 函数 / 常量 | 位置（`healthcheck.py`） |
|---|---|---|
| recent 处理总流程（含 --status-summary 分支） | `command_recent` | `588-712` |
| `--summary` 与 `--status-summary` 互斥，最先检查 | 互斥分支 + `die` | `591-597` |
| 筛选值校验顺序：status → URL → reason → since → until（含窗口次序） | `validate_status_filter` / `validate_target_url` / `validate_reason_filter` / `validate_time_bound_filter` | 调用点 `602` / `607-608` / `613` / `620-621`，窗口次序 `622-631` |
| 三个库内筛选取交集（AND），URL 原始字符串精确匹配，reason 只看保存值 | `build_recent_query` / `RECENT_FILTER_CLAUSES` | `489-513` / `120-124`，调用点 `683-691` |
| 一律按 id 倒序；无时间边界时 SQL 内 `LIMIT ?` | `SELECT_SQL` | `110-116`，执行点 `692-693` |
| 默认最多 5 条；limit 必须为正整数 | `DEFAULT_LIMIT` / `positive_limit` | `32` / `142-150` |
| 时间边界生效时先窗口过滤（UTC 时刻比较、端点包含）再限量 | `filter_rows_by_since` | `279-306`，调用点 `696-698` |
| 状态计数只依据保存的 status，不从 reason/http_status 推断；空集三个计数为 0 | `build_status_summary` / `STATUS_SUCCESS` / `STATUS_FAILURE` | `554-566` / `39` / `40` |
| 单行紧凑 JSON 输出；三种形态共用呈现入口 | `render_recent_result` | `569-585`（status-summary 分支 `581-582`），调用点 `711` |
| 缺库（含父目录缺失）视为空，不创建 | 路径存在性分支 | `639-641` |
| 无历史表视为空、不建表；缺字段以退出码 2 报缺列 | 表缺失分支 / `ensure_checks_columns` | `670-672` / `381-393`（调用点 `676`） |
| 只读打开、可读不可写也能查 | `?mode=ro` URI | `649-653` |
| 错误统一出口（退出码 2、stderr、stdout 空） | `die` | `126-129` |
| `--status-summary` 参数定义（开关，与 --summary 互斥见帮助文案） | `build_parser` 中 recent 子解析器 | `797-803` |
