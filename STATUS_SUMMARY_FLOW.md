# `recent --status-summary` 状态计数摘要流程说明

本说明描述 `healthcheck.py` **当前已实现**的 `recent --status-summary` 行为：
从公开命令行参数进入，沿「互斥检查 → 筛选校验 → 数据库只读查询 →（有时间边界时）
Python 侧时刻过滤与限量 → 状态计数 → 单行 JSON 输出」讲清每一步，并标注结论
对应的函数与源码位置（行号以当前 `healthcheck.py` 为准）。普通 `recent` 的完整
通用流程（五个筛选、八种 WHERE 组合、各类错误通道）见 `RECENT_QUERY_FLOW.md`，
本文只聚焦 `--status-summary` 这一输出形态，不重复其全部细节。

- 入口：`main`（`healthcheck.py:809-812`）→ `command_recent`
  （`healthcheck.py:588-712`）；`--status-summary` 参数定义在 recent 子解析器
  （`healthcheck.py:797-803`）。
- 该查询**不发送网络请求、不创建文件/目录/表、不改动已有记录**；数据库以只读
  URI 打开，可读但不可写的库仍可查询（见第 8 节）。

> 文中所有命令输出块均为**依据当前源码逐条推导的预期值**，不是某次实际运行的
> 转存；推导所依据的测试为
> `test_recent_status_summary_stored_status.py`（本文固定样本与该测试顶部
> `REC1/REC2/REC3` 完全一致）与 `test_recent_status_summary.py`。可照第 4 节
> 在空目录准备本地 SQLite 库，再运行第 5 节命令自行核对。

---

## 1. 它统计的是哪一批记录

**状态摘要只统计普通 `recent`（不带任何摘要开关）在相同筛选与相同 `--limit`
下本会返回的那同一批记录**，不另发查询、不统计被 limit 截掉的记录，也不统计
被筛选排除的记录。三种输出形态（记录数组 / 耗时摘要 / 状态摘要）在
`render_recent_result`（`healthcheck.py:569-585`）中接收的是同一个 `rows`：

```python
if summary:
    payload = build_elapsed_summary(rows)
elif status_summary:
    payload = build_status_summary(rows)
else:
    payload = [row_to_record(row) for row in rows]
```

`rows` 的形成规则与普通 `recent` 完全一致（`command_recent`，
`healthcheck.py:634-698`；SQL 组装见 `build_recent_query`，
`healthcheck.py:489-513`）：

1. **先取各筛选条件的交集**：`--url`、`--status`、`--reason` 三个库内筛选各自
   生成一个 AND 条件片段（`RECENT_FILTER_CLAUSES`，`healthcheck.py:120-124`），
   由 `build_recent_query` 按 url → status → reason 的固定顺序拼为 WHERE 子句，
   无条件时为空。
   - **URL 沿用命令行原始字符串精确匹配**：SQL 条件为 `url = ?`
     （`healthcheck.py:121`），不做任何归一化；`--url` 仅借用
     `validate_target_url`（`healthcheck.py:309-359`）做本地合法性校验，
     匹配仍用原始字符串。
   - `--status`、`--reason` 都是对保存值的等值匹配（`status = ?` /
     `reason = ?`），不存在任何跨列推断。
2. **再按 id 倒序限量**：无时间边界时 SQL 为
   `… ORDER BY id DESC LIMIT ?`（模板 `SELECT_SQL`，`healthcheck.py:110-116`；
   执行点 `healthcheck.py:692-693`），排序键只有 id；默认 limit 为 **5**
   （`DEFAULT_LIMIT`，`healthcheck.py:32`；`--limit` 校验 `positive_limit`，
   `healthcheck.py:142-150`），即默认最多统计五条。
3. **有时间边界时先完成时间窗口筛选，再限量**：`--since` / `--until` 任一被
   给出时（`time_filter_active`，`healthcheck.py:634`），SQL **不带 LIMIT**
   （`build_recent_query(..., sql_limit=False)`，`healthcheck.py:690`），先取
   出符合 url/status/reason 的全部记录，再由 `filter_rows_by_since`
   （`healthcheck.py:279-306`）在 Python 侧逐条解析 `checked_at` 并按时刻过滤，
   **最后** `kept[:limit]`（`healthcheck.py:306`）截取。因此时间窗口与 limit
   的次序固定为「先窗口、后限量」，limit 永远作用在全部筛选（含窗口）的交集
   之后。时间比较沿用现有规则（与普通 `recent`、`--summary` 相同）：按 UTC
   **时刻**比较而非字符串比较（`parse_utc_timestamp`，`healthcheck.py:201-222`，
   `Z` 与 `+00:00`、显式零小数与省略小数视为同一时刻），窗口两端均包含
   （`checked_at >= since` 且 `checked_at <= until`，相等命中；
   `healthcheck.py:301-304`）。

本文第 4 节的固定样本三条记录共用同一 `checked_at`，故第 5 节两条验收命令都
不带时间边界；时间窗口下「先过滤、后限量、再计数」的行为由
`test_recent_status_summary.py::test_time_window_z_and_offset_equivalent`
覆盖（该测试的样本上，
`--since 2026-10-02T04:40:02Z --until 2026-10-02T04:40:04.000000+00:00`
预期为 `{"count":3,"success_count":1,"failure_count":2}`）。

## 2. 输出形态：只有三个整数字段的一行 JSON

计数由 `build_status_summary`（`healthcheck.py:554-566`）完成：

```python
def build_status_summary(rows):
    """状态摘要：只按记录保存的 status 字段分类计数，绝不从 reason 或
    http_status 推断；空集三个计数均为 0。
    """
    return {
        "count": len(rows),
        "success_count": sum(1 for row in rows if row[4] == STATUS_SUCCESS),
        "failure_count": sum(1 for row in rows if row[4] == STATUS_FAILURE),
    }
```

- 输出为**一行紧凑 JSON 对象加一个换行**
  （`json.dumps(payload, separators=(",", ":"), ensure_ascii=False)`，
  `healthcheck.py:585`；换行来自 `print`），不是 JSON 数组，也没有耗时统计
  字段（`min_elapsed_ms` 等属于 `--summary`，两者互斥，见第 7 节）。
- 对象**恰好包含三个字段，全部为整数**，键顺序固定：
  - `count`：这批记录的总数（`len(rows)`）；
  - `success_count`：其中**保存的 `status` 等于 `success`** 的行数
    （`STATUS_SUCCESS`，`healthcheck.py:39`；SELECT 列序中 `status` 是
    `row[4]`，列清单见 `SELECT_SQL`，`healthcheck.py:111-112`）；
  - `failure_count`：其中**保存的 `status` 等于 `failure`** 的行数
    （`STATUS_FAILURE`，`healthcheck.py:40`）。
- 建表约束保证 status 只能是 `'success'` / `'failure'`
  （`CREATE_TABLE_SQL` 的 CHECK，`healthcheck.py:86`），所以对任何合法库恒有
  **`success_count + failure_count == count`**。

### 2.1 分类依据只是保存的 status，不从 reason 或 http_status 重新判断

两个计数器只读 `row[4]`（保存的 status）；`reason`（`row[6]`）与
`http_status`（`row[5]`）完全不参与分类。具体而言：

- `reason = 'timeout'` **不会**把记录改判为失败；
- `http_status = 500` **不会**把记录改判为失败；
- `reason = 'ok'`、`http_status = 200` **不会**把记录改判为成功。

这正是第 4 节样本刻意安排字段含义相互矛盾的原因：若错误地按 reason/http_status
推断，三条会被算成「1 成功 2 失败」；按保存的 status 计数才是「2 成功
1 失败」（第 5.1 节）。回归依据见
`test_recent_status_summary_stored_status.py`（该文件的全部用例都只按保存值
断言），`--reason` 筛选本身同样只按保存值等值匹配、绝不推断
（`validate_reason_filter`，`healthcheck.py:175-198`）。

## 3. 端到端流程（与普通 recent 相同的骨架，只换呈现入口）

`main` 解析参数后调用 `command_recent`（`healthcheck.py:588-712`），步骤顺序：

1. **互斥检查最先**：`--summary` 与 `--status-summary` 同用立即 `die`（退出码
   2），发生在一切筛选值校验与数据库访问之前（`healthcheck.py:593-597`，详见
   第 7 节）。
2. **依次校验筛选值**：`--status`（调用点 `healthcheck.py:602`，校验函数
   `153-172`）→ `--url`（`607-608`）→ `--reason`（`613`，函数 `175-198`）→
   `--since`（`620`）→ `--until`（`621`，共用
   `validate_time_bound_filter`，`253-276`）→ 双端点窗口次序（`622-631`）。
   非法值在读取数据库前即以退出码 2 拒绝（stdout 为空，原因写 stderr）。
3. **数据库路径不存在 → 空历史快路径**（`healthcheck.py:639-641`）：连父目录
   一起不存在也相同，不打开 sqlite、不创建任何东西，直接以空列表走
   `render_recent_result` → `build_status_summary([])`，输出三个零计数。
4. **路径是目录 → 退出码 2**（`healthcheck.py:644-645`）；这不是零计数情形。
5. **以只读 URI 打开**：绝对路径 file URI 加 `?mode=ro`
   （`healthcheck.py:649`），可读不可写的文件也能查询，且不会生成
   `-wal`/`-journal`。recent **从不调用** check 专用的、会建库建表的
   `open_database`（`healthcheck.py:362-378`）。
6. **识别历史表**：查 `sqlite_master`，表名按大小写不敏感识别
   （`CHECKS`/`Checks` 同 `checks`，`healthcheck.py:658-669`）；没有历史表
   （空库或仅有其他表）时 `rows = []`（`670-672`），不报错、不建表。有表则先
   `ensure_checks_columns`（`healthcheck.py:381-393`，调用点 `676`）核对七个
   字段，缺列以退出码 2 报告，不误判为空历史。
7. **取数**：`build_recent_query` 组装查询（`683-691`）；无时间边界走
   `conn.execute(sql, (*params, args.limit))`（`692-693`），有时间边界取全部
   候选后交 `filter_rows_by_since` 过滤并 `kept[:limit]`（`694-698`）。
8. **计数输出**：`render_recent_result(rows, args.summary,
   args.status_summary)`（`healthcheck.py:711`）分派到 `build_status_summary`，
   打印单行 JSON，处理函数返回 `0`（`712`），stderr 为空。

## 4. 固定样本（复用 `test_recent_status_summary_stored_status.py`，3 条）

三条记录共用同一目标、同一 `checked_at`、零耗时；**status 与
reason/http_status 的组合在含义上刻意相互矛盾，但每行都满足 checks 表的全部
约束**（样本常量见测试文件 `REC1/REC2/REC3`、`ALL_RECORDS`）：

| id | url | checked_at（保存的原始字符串） | elapsed_ms | **status（保存值）** | http_status | reason |
|----|-----|------|-----|------|------|------|
| 1 | `http://127.0.0.1:8765/health` | `2026-10-05T00:00:00Z` | 0 | **success** | null | **timeout** |
| 2 | `http://127.0.0.1:8765/health` | `2026-10-05T00:00:00Z` | 0 | **success** | **500** | http_status |
| 3 | `http://127.0.0.1:8765/health` | `2026-10-05T00:00:00Z` | 0 | **failure** | **200** | **ok** |

- id 1：保存 **success**，却配通常意味着失败的 `timeout` 与 null 状态码；
- id 2：保存 **success**，却配 HTTP **500**；
- id 3：保存 **failure**，却配通常意味着成功的 `reason=ok` 与 HTTP **200**。

### 4.1 本地 SQLite 准备方法

在一个**空目录**中执行（只用 Python 3 标准库，直接建库，不发任何网络请求；
建表语句与产品代码 `CREATE_TABLE_SQL`（`healthcheck.py:80-91`）及测试文件的
`SCHEMA_SQL` 逐字一致，数据与测试夹具 `build_sample_db` 写入的内容等价）：

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

`monitor.sqlite` 由上面的准备脚本创建；随后的 `recent --status-summary`
查询本身不创建或修改它（见第 8 节）。

## 5. 两条可直接使用的验收查询（输出为源码推导的预期值）

### 5.1 查询一：不带任何筛选，三条全量计数

```sh
python healthcheck.py --db monitor.sqlite recent --status-summary
```

无筛选、无时间边界：SQL 为 `SELECT … FROM checks ORDER BY id DESC LIMIT ?`，
默认 limit 5 覆盖全部三条（id 倒序为 3、2、1，不截断）。按保存的 status 计数：
id 1、id 2 为 success，id 3 为 failure。

**预期 stdout（一行 JSON；预期退出码 `0`、stderr 为空；对应
`test_counts_use_stored_status_only`）：**

```json
{"count":3,"success_count":2,"failure_count":1}
```

若改按 reason/http_status 推断（timeout→失败、500→失败、ok/200→成功）会得到
1 成功 2 失败；实际输出 2 成功 1 失败，证明分类只看保存的 status。

### 5.2 查询二：追加 `--reason timeout`

```sh
python healthcheck.py --db monitor.sqlite recent --status-summary --reason timeout
```

SQL 为 `SELECT … FROM checks WHERE reason = ? ORDER BY id DESC LIMIT ?`，按
**保存的 reason 精确匹配**只命中 id 1（id 2 的 reason 是 `http_status`、
id 3 是 `ok`）；id 1 保存的 status 是 success——尽管 timeout 通常意味着失败，
也不得改判。

**预期 stdout（一行 JSON；预期退出码 `0`、stderr 为空；对应
`test_reason_timeout_filter_matches_success_record`）：**

```json
{"count":1,"success_count":1,"failure_count":0}
```

### 5.3 同一样本上的补充预期（均为退出码 0、stderr 空）

这些组合进一步固定「先取交集、再按 id 倒序限量、只按保存 status 计数」的口径，
均可在同一 `monitor.sqlite` 上核对：

| 命令追加参数 | 预期输出 | 对应用例 |
|---|---|---|
| `--limit 2` | `{"count":2,"success_count":1,"failure_count":1}`（id 倒序取 id 3 failure、id 2 success） | `test_limit_2_counts_latest_two_by_id_desc` |
| `--status failure --reason ok` | `{"count":1,"success_count":0,"failure_count":1}`（仅 id 3 同时满足；ok/200 不改判） | `test_status_failure_and_reason_ok_intersection` |
| `--status success --reason ok` | `{"count":0,"success_count":0,"failure_count":0}`（交集为空） | `test_status_success_and_reason_ok_intersection_empty` |

## 6. 无匹配、缺库、无历史表、空表：三个计数均为零，退出码 0

以下「合法但没有记录」的情形都以空记录集走同一呈现入口
`render_recent_result([])` → `build_status_summary([])`
（`healthcheck.py:569-585, 554-566`），输出**逐字节相同**，退出码 `0`、
stderr 为空、不创建任何东西：

```json
{"count":0,"success_count":0,"failure_count":0}
```

| 情形 | 空集来源 | 源码位置 |
|---|---|---|
| 筛选（含时间窗口过滤后）无匹配 | SELECT / Python 过滤返回 0 行 | `healthcheck.py:692-698` |
| 数据库文件不存在（父目录也不存在） | 打开 sqlite 前的存在性快路径 | `healthcheck.py:639-641` |
| 有效库但没有历史表（空库或仅有其他表） | `checks_table is None` → `rows = []` | `healthcheck.py:670-672` |
| 历史表存在但为空表 | 查询返回 0 行 | `healthcheck.py:692-693` |

注意与**错误**情形区分：`--db` 指向目录、文件不是有效 SQLite 库、历史表缺字段
分别在 `healthcheck.py:644-645`、`701-704`、`676` 经 `die` 以退出码 2 拒绝
（stdout 为空），不输出零计数摘要。

## 7. `--summary` 与 `--status-summary` 互斥

同时给出两个摘要开关时：

```sh
python healthcheck.py --db monitor.sqlite recent --summary --status-summary
```

- **预期退出码 `2`**；
- **stdout 完全为空**；
- **stderr 指出互斥**，预期为（前缀由 `die` 统一添加，`healthcheck.py:126-129`）：

```text
healthcheck: error: 参数错误：--summary 与 --status-summary 互斥，一次查询只能选用其中一种摘要
```

该检查是 `command_recent` 中的**第一步**（`healthcheck.py:593-597`），先于
`--status` 校验（调用点 `602`）、URL/reason/时间边界校验（`607-631`）与一切
数据库访问（缺库快路径始于 `639`，只读打开始于 `649`）。因此即使数据库路径
不存在、或同时给出非法筛选值，也只报互斥且不创建任何文件。对应
`test_recent_status_summary_stored_status.py::test_summary_and_status_summary_are_mutually_exclusive`
与
`test_recent_status_summary.py::test_mutual_exclusion_checked_before_database_access`
（后者断言缺失的目录不会被创建）。唯一更早的拒绝发生在 argparse 解析阶段
本身（如非法 `--limit`、无法识别的参数），那时处理函数尚未被调用。

## 8. 只读、无网络、无副作用保证

- **不发网络请求**：`recent` 路径不调用 `probe_once`
  （`healthcheck.py:413-437`，仅 `check` 使用）；`--url` 只做本地合法性校验
  与保存字符串匹配，`--since`/`--until` 只做本地时间解析与比较。
- **不创建文件或目录**：缺库（含父目录缺失）直接走空结果快路径
  （`healthcheck.py:639-641`），不调用会建库的 `open_database`。
- **不创建表、不改动已有记录**：库以 `?mode=ro` 只读 URI 打开
  （`healthcheck.py:649-651`），全程只有 SELECT（含对 `sqlite_master` 的
  查询）；无历史表时仅置空结果，不补建；有时间边界时也只在 Python 内过滤已取
  出的行。
- **可读不可写的数据库仍可查询**：只读 URI 不需要写权限，也不产生
  `-wal`/`-journal`（`test_recent_status_summary.py::test_readonly_database_file`
  把样本库权限改为 `0444` 后仍得到正确计数；异写表名 `CHECKS` 的只读查询见
  同文件 `test_uppercase_table_name`）。
- `test_recent_status_summary_stored_status.py` 的每个用例都用
  `snapshot_state` 在查询前后比对目录文件集合、`sqlite_master` 表结构与全部表
  的全部行，确认上述只读性（包括互斥被拒绝时状态同样不变）。

## 9. 不变性声明

本文只是说明文档：**程序、数据库结构、现有测试与公开入口的行为均保持不变**。

- 普通 `recent` 仍输出七字段记录数组，`--summary` 仍输出
  `count/min_elapsed_ms/max_elapsed_ms/avg_elapsed_ms` 耗时摘要；三者只是
  `render_recent_result`（`healthcheck.py:569-585`）对同一 `rows` 的不同呈现。
- checks 表结构（`CREATE_TABLE_SQL`，`healthcheck.py:80-91`）、退出码约定
  （成功 0 / 已记录的探测失败 1 / 参数或数据库错误 2）与全部筛选规则不变。

## 10. 关键结论到源码位置的对照

| 结论 | 函数 / 常量 | 位置 |
|---|---|---|
| recent 处理总流程（含 --status-summary） | `command_recent` | `588-712` |
| `--status-summary` 公开入口定义 | recent 子解析器参数 | `797-803` |
| 摘要只统计普通 recent 在相同筛选与 limit 下返回的同一批 `rows` | `render_recent_result` 分派 | `569-585`，调用点 `711` |
| 三个计数：`count=len(rows)`，success/failure 各按 `row[4]` 统计 | `build_status_summary` | `554-566` |
| 分类只看保存的 status，不从 reason/http_status 推断 | `build_status_summary` / status 常量 | `554-566` / `39-40` |
| status 取值约束（故两计数之和恒等于 count） | `CREATE_TABLE_SQL` CHECK | `86` |
| 先取 url/status/reason 交集、再 `ORDER BY id DESC` | `SELECT_SQL` / `RECENT_FILTER_CLAUSES` / `build_recent_query` | `110-116` / `120-124` / `489-513` |
| 默认最多五条、limit 须为正整数 | `DEFAULT_LIMIT` / `positive_limit` | `32` / `142-150` |
| URL 按原始字符串精确匹配（校验不改变匹配值） | `url = ?` / `validate_target_url` | `121` / `309-359`，调用点 `607-608` |
| 有时间边界时先取全部候选、按时刻窗口过滤、最后 `kept[:limit]` | `filter_rows_by_since` / `time_filter_active` | `279-306` / `634`，调用点 `694-698` |
| UTC 时刻比较；两端包含（`>= since`、`<= until`） | `parse_utc_timestamp` / 边界比较 | `201-222` / `301-304` |
| 一行紧凑 JSON（仅三个整数字段、键序固定） | `json.dumps(..., separators=(",", ":"))` | `585` |
| 无匹配 / 缺库 / 无历史表 / 空表 → 三个 0、退出码 0 | 空集各分支 → `build_status_summary([])` | `639-641`、`670-672`、`692-698`、`554-566` |
| `--summary` 与 `--status-summary` 互斥：退出码 2、stdout 空、stderr 指出互斥，先于筛选校验与数据库访问 | `command_recent` 互斥检查 + `die` | `593-597` / `126-129` |
| 缺库不创建文件/目录；只读 URI 打开，不可写也能查、不产生 wal/journal | 存在性分支 / `?mode=ro` | `639-641` / `649-651` |
| 历史表名大小写不敏感识别；无历史表不建表 | `sqlite_master` 判断与空集分支 | `658-672` |
| 网络探测与建库写入仅属于 check，recent 不触碰 | `probe_once` / `open_database` | `413-437` / `362-378` |
