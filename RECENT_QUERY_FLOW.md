# `recent` 查询流程说明（单次探测之外的历史读取）

本说明只描述 `healthcheck.py` **当前已实现**的 `recent` 子命令流程，不新增任何行为。
内容从公开命令行参数进入，沿「目标校验 → 数据库读取 → JSON 输出」讲清每一步，
并标注结论对应的函数或 SQL 常量（行号以当前 `healthcheck.py` 为准）。
文中两个验收示例已用 `test_recent_summary.py` 的固定样本实际运行核对，
退出码、stdout、stderr 均与记录一致；现有测试套件（26 项）保持通过。

- 入口函数：`main`（`healthcheck.py:382`）→ `command_recent`（`healthcheck.py:243`）
- `recent` **不发送网络请求、不创建文件/目录/表、不改动已有记录**；数据库以只读
  URI 打开，可读但不可写的库仍可查询。

---

## 1. 公开参数

命令形式（参数定义见 `build_parser` 中 recent 子解析器，`healthcheck.py:358-377`）：

```sh
python healthcheck.py --db <数据库路径> recent [--url <目标URL>] [--limit <正整数>] [--summary]
```

| 参数 | 是否必填 | 默认 | 约束与语义 | 代码位置 |
|---|---|---|---|---|
| `--db` | 必填（全局） | 无 | SQLite 数据库文件路径 | `healthcheck.py:345` |
| `--url` | 可选 | `None` | 给出时只返回该目标的记录；省略时查询**全部目标** | `healthcheck.py:360-364` |
| `--limit` | 可选 | `5`（`DEFAULT_LIMIT`，`healthcheck.py:25`） | 正整数；`0`、负数、`1.5`、非数字均被拒绝 | `positive_limit`，`healthcheck.py:91-99` |
| `--summary` | 可选开关 | 关 | 输出这批记录的耗时摘要，而非记录数组 | `healthcheck.py:371-376` |

`--url` 的合法规则与 `check` 完全一致，由 `validate_target_url`
（`healthcheck.py:102-152`）校验：仅 `http` 协议、主机必须是 `127.0.0.1`、
必须显式指定 `1-65535` 端口；允许路径与查询参数；不接受用户信息（userinfo），
原始字符串中只要出现 `#` 即按片段（fragment）拒绝；含空白/控制字符、
结构无法解析、主机或端口非法也一律拒绝。

**注意：校验只用于判定输入是否合法；数据库匹配使用的是命令行上的原始字符串，
不做任何规范化。**

---

## 2. 端到端流程

`main` 解析参数后调用 `args.handler`，对 `recent` 即 `command_recent`
（`healthcheck.py:243-337`）。步骤严格按以下顺序发生：

1. **先校验筛选 URL**（`healthcheck.py:249-250`）。
   提供了 `--url` 时先调用 `validate_target_url(args.url)`；非法 URL 立即经
   `die`（`healthcheck.py:75-78`）输出 stderr 并以退出码 `2` 结束。
   这一步先于一切数据库路径判断，因此**合法 limit 下，非法 URL 优先于数据库
   路径错误**（目录、缺库等都不会再被报告）。

2. **数据库路径不存在 → 视为空历史**（`healthcheck.py:254-259`）。
   `os.path.exists` 为假时（文件不存在，**连父目录一起不存在也相同**），
   普通查询输出 `[]`，摘要输出常量 `NULL_SUMMARY_JSON`
   （`healthcheck.py:69-72`），退出码 `0`。此分支不打开 sqlite，不创建任何东西。

3. **路径是目录 → 错误**（`healthcheck.py:262-263`）。
   经 `die` 报「路径是一个目录，不是 SQLite 数据库文件」，退出码 `2`。

4. **以只读模式打开数据库**（`healthcheck.py:267-271`）。
   打开的是文件绝对路径的 file URI 并附 `?mode=ro`：
   `pathlib.Path(...).as_uri() + "?mode=ro"`。因此：
   - 不执行 `CREATE TABLE`，不会创建或修改数据库文件，也不产生 `-wal`/`-journal`；
   - 权限为「可读不可写」的文件照样可以查询；
   - 无法打开（如读取权限不足）时经 `die` 结束，退出码 `2`。
   注意：`check` 使用的 `open_database`（`healthcheck.py:155-166`，会建库建表）
   **recent 从不调用**。

5. **判断表是否存在并执行查询**（`healthcheck.py:274-292`）。
   先查 `sqlite_master`：有效库中没有 `checks` 表（空库或仅有其他表）时，
   `rows = []`，不报错、不建表。否则：
   - 带 `--url`：执行 `SELECT_BY_URL_SQL`（`healthcheck.py:60-66`），
     `WHERE url = ?` 绑定**原始 `args.url`**，`ORDER BY id DESC LIMIT ?`；
   - 不带 `--url`：执行 `SELECT_SQL`（`healthcheck.py:53-58`），
     无 `WHERE`，同样 `ORDER BY id DESC LIMIT ?`。

   两条 SQL 的选择列顺序均为
   `id, url, checked_at, elapsed_ms, status, http_status, reason`。
   查询期 sqlite 错误（文件不是有效 SQLite 库、`checks` 表缺少查询所需字段等）
   由 `except sqlite3.Error` 捕获（`healthcheck.py:295-297`），经 `die` 以
   退出码 `2` 结束。

6. **输出 JSON**（单行，紧凑分隔，`ensure_ascii=False`，末尾一个换行）：
   - `--summary`：见第 5 节（`healthcheck.py:299-322`）；
   - 普通查询：把每行按七字段映射为记录对象数组
     （`healthcheck.py:324-336`），`json.dumps(..., separators=(",", ":"))`
     输出（`healthcheck.py:336`）。无记录时输出 `[]`。

两种成功形态退出码均为 `0`，stderr 为空。

---

## 3. 固定样本（复用 `test_recent_summary.py`）

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
  对应 `SELECT_BY_URL_SQL`（`healthcheck.py:60-66`）。
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

**摘要统计口径确认**（实现：`healthcheck.py:299-322`）：摘要统计的就是
**同条件 `recent`（不带 `--summary`）会返回的同一记录子集**——同样的原始 URL
精确筛选、同样按 id 倒序、同样的 limit 截取；代码对这批 `rows` 直接取
`row[3]`（即 `elapsed_ms`）计算，并不另外发起任何查询。因此它：

- **不是全历史统计**：id 1（90 ms）因 `LIMIT 3` 被排除，不计入；
- **不是时间窗口统计**：没有任何时间条件，`checked_at` 只作为输出字段；
- **不是仅成功记录**：id 6（failure，7 ms）与 id 3（failure，0 ms）都计入；
- **零耗时参与统计**：`min_elapsed_ms` 为 0（id 3），count 仍为 3；
- **平均值不取整**：`(7 + 2 + 0) / 3 = 3.0`，以 JSON 数字原样输出
  （`sum(...) / count` 的浮点除法，`healthcheck.py:310`），故序列化为 `3.0`
  而非 `3`；max 为 7（id 6）。

无记录时（缺库、无 `checks` 表、空表、合法目标无匹配），摘要为
`count: 0` 且三个耗时字段为 `null`：缺库快路径输出常量 `NULL_SUMMARY_JSON`
（`healthcheck.py:69-72`），其余路径构造等价对象（`healthcheck.py:312-321`）。

---

## 6. 空结果与错误的区别

两种情况退出码、输出通道完全不同。

### 6.1 空结果：退出码 0，正常输出

| 情形 | 普通查询 stdout | 摘要 stdout | 代码位置 |
|---|---|---|---|
| 数据库文件不存在（父目录也不存在） | `[]` | `{"count":0,"min_elapsed_ms":null,"max_elapsed_ms":null,"avg_elapsed_ms":null}` | `healthcheck.py:254-259` |
| 有效数据库但没有 `checks` 表（含仅有其他表） | `[]` | 同上（count 0、三个 null） | `healthcheck.py:282-284` + `312-321` |
| `checks` 表存在但为空表 | `[]` | 同上 | 查询返回 0 行，`healthcheck.py:299-321` |
| 合法目标但无匹配记录 | `[]` | 同上 | `SELECT_BY_URL_SQL` 返回 0 行 |

这些情形 stderr 均为空，且不创建文件/目录、不补建 `checks` 表，其他表与数据
保持不变。

### 6.2 错误：退出码 2，stdout 为空，原因写入 stderr

| 情形 | 报告位置 / 实测信息要点 |
|---|---|
| 非法 `--url`（非 http、非 127.0.0.1、缺端口、端口越界、userinfo、含 `#`、空白或结构非法等） | `validate_target_url` → `die`，前缀 `healthcheck: error:`，并回显非法输入（`healthcheck.py:102-152, 249-250`） |
| 非法 `--limit`（0、负数、`1.5`、非数字） | argparse 在进入 `command_recent` **之前**拒绝，退出码 2、stdout 空、用法与原因写 stderr（`positive_limit`，`healthcheck.py:91-99`） |
| `--db` 路径是目录 | `healthcheck.py:262-263`，信息含「目录」 |
| 文件存在但不是有效 SQLite 数据库 | 查询期错误，实测为 `file is not a database`（`healthcheck.py:295-297`） |
| 读取权限不足 | 只读打开/查询失败，实测为 `unable to open database file`（`healthcheck.py:267-271, 295-297`） |
| `checks` 表缺少查询所需字段 | 查询期错误，实测为 `no such column: checked_at`（`healthcheck.py:295-297`） |

优先级：argparse 先解析 `--limit`，故 limit 非法时根本不会进入处理函数；
**在 limit 合法的前提下，`command_recent` 先校验 URL、后判断数据库路径，
所以非法 URL 优先于数据库路径错误**（例如非法 URL 配合目录路径，只报 URL 错误）。
所有 `die` 错误都只有 stderr、无 Python 回溯（`die`，`healthcheck.py:75-78`）。

---

## 7. 只读、无副作用保证

- **不发送网络请求**：`recent` 路径不调用 `probe_once`（`healthcheck.py:169-193`），
  URL 仅做本地合法性校验与字符串匹配。回归用例
  `test_summary_makes_no_network_requests` 在本地起服务器核对请求数为 0。
- **不创建文件或目录**：数据库/父目录缺失时直接输出空结果
  （`healthcheck.py:254-259`），不调用会建库的 `open_database`。
- **不创建表、不改动记录**：库以 `?mode=ro` 打开（`healthcheck.py:267`），
  全程只有 `SELECT`（含对 `sqlite_master` 的查询）；无 `checks` 表时仅置空结果。
  测试通过前后目录文件集合与全表内容快照比对确认无变化。
- **可读但不可写的数据库仍可查询**：只读 URI 不需要写权限，也不生成
  `-wal`/`-journal` 旁路文件。

## 8. 保持不变的既有语义

- 默认 `--limit 5`（`DEFAULT_LIMIT`）；未给 `--url` 时跨全部目标按 id 倒序
  返回（`SELECT_SQL`）。固定样本下无参数 `recent` 返回 id 7、6、5、4、3。
- `check` 的探测与写入行为不变：`probe_once` 只发一次 GET、不跟随重定向，
  成功/失败经 `INSERT_SQL` 落库后才输出（`command_check`，
  `healthcheck.py:200-240`）；建库建表仍只由 `check` 的 `open_database` 完成。
- 记录七字段、表结构（`CREATE_TABLE_SQL`，`healthcheck.py:35-46`）与退出码
  约定（成功 0 / 已记录的探测失败 1 / 参数或数据库错误 2）均不变。

## 9. 结论到源码位置的对照

| 结论 | 函数 / 常量 | 位置 |
|---|---|---|
| recent 处理总流程 | `command_recent` | `healthcheck.py:243-337` |
| URL 合法性规则（且先于 DB 路径执行） | `validate_target_url` | `healthcheck.py:102-152`，调用点 `249-250` |
| limit 必须为正整数、默认 5 | `positive_limit` / `DEFAULT_LIMIT` | `healthcheck.py:91-99` / `25` |
| 缺库（含父目录缺失）视为空，不创建 | 路径存在性分支 | `healthcheck.py:254-259` |
| 目录路径报错（退出码 2） | 目录分支 + `die` | `healthcheck.py:262-263` |
| 只读打开、不可写也能查 | `?mode=ro` URI | `healthcheck.py:267-271` |
| 无 `checks` 表视为空、不建表 | `sqlite_master` 判断 | `healthcheck.py:276-284` |
| 全部目标：按 id 倒序限量 | `SELECT_SQL` | `healthcheck.py:53-58`，执行点 `292` |
| 指定目标：原始 URL 精确匹配后按 id 倒序限量 | `SELECT_BY_URL_SQL` | `healthcheck.py:60-66`，执行点 `288-290` |
| 摘要统计同条件记录子集；失败与零耗时计入；均值不取整 | 摘要分支 | `healthcheck.py:299-322` |
| 空摘要固定输出（count 0、三个 null） | `NULL_SUMMARY_JSON` | `healthcheck.py:69-72` |
| 完整七字段记录数组 / `[]` 输出 | 记录映射与 JSON 输出 | `healthcheck.py:324-336` |
| 错误统一出口（退出码 2、stderr、stdout 空） | `die` | `healthcheck.py:75-78` |
| 网络探测与建库/写入仅属于 check（recent 不触碰） | `probe_once` / `open_database` / `INSERT_SQL` | `healthcheck.py:169-193` / `155-166` / `48-51` |
