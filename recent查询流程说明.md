# `recent` 查询流程说明（healthcheck.py）

本文可独立阅读，描述当前版本 `healthcheck.py` 的 `recent` 子命令从命令行参数到
JSON 输出的完整流程，并给出可复现的验收示例。本文**仅说明现状，不新增任何行为**；
产品代码、`README.md` 与现有测试均未因本文修改。

- 产品代码：`healthcheck.py`
- 固定样本与回归断言来源：`test_recent_summary.py`（摘要、空结果、错误、网络静默）、
  `test_recent_url_filter.py`（URL 精确筛选、错误优先级）
- 下文行号均对应当前工作区中的 `healthcheck.py`；样本行号对应
  `test_recent_summary.py`。

---

## 1. 公开入口与参数

入口形式：

```
python healthcheck.py --db <数据库路径> recent [--url <目标URL>] [--limit <正整数>] [--summary]
```

参数在 `build_parser()` 中定义（`healthcheck.py:340-379`），`recent` 部分见
`healthcheck.py:358-377`，解析后由 `main()` 分发到 `command_recent(args)`
（`healthcheck.py:382-385`）：

| 参数 | 含义 | 默认 / 约束 | 定义位置 |
|---|---|---|---|
| `--db`（全局，必填） | SQLite 数据库文件路径 | 字符串 | `healthcheck.py:345` |
| `--url` | 仅查询该目标的记录 | 省略＝查询全部目标 | `healthcheck.py:360-364` |
| `--limit` | 返回（及统计）的最大条数 | 默认 `5`（常量 `DEFAULT_LIMIT`，`healthcheck.py:25`）；必须是正整数，由 `positive_limit()` 校验，`healthcheck.py:91-99` | `healthcheck.py:366-370` |
| `--summary` | 不输出记录数组，改为输出这批记录的耗时摘要 | 开关，默认关 | `healthcheck.py:371-376` |

被查询的历史记录由 `check` 子命令写入：`probe_once()` 发一次 GET
（`healthcheck.py:169-193`），经 `INSERT_SQL`（`healthcheck.py:48-51`）落库，
表结构见 `CREATE_TABLE_SQL`（`healthcheck.py:35-46`）。`recent` 只读取这张
`checks` 表，七个列为 `id, url, checked_at, elapsed_ms, status, http_status,
reason`。

---

## 2. 端到端流程（函数与 SQL 常量对照）

全部逻辑在 `command_recent()`（`healthcheck.py:243-337`），顺序固定：

1. **校验筛选 URL（若提供）** — `healthcheck.py:249-250`
   调用 `validate_target_url(args.url)`（`healthcheck.py:102-152`），规则与
   `check` 完全相同：仅 `http` 协议、主机必须 `127.0.0.1`、必须显式给出
   `1-65535` 端口，允许路径与查询参数，拒绝 userinfo 与原始 `#` 片段。
   校验**只判定合法性**；合法时后续匹配使用用户输入的**原始字符串**，不做任何
   规范化（`healthcheck.py:246-248` 注释明确这一点）。
   非法 URL 经 `die()`（`healthcheck.py:75-78`）写入 stderr 并以退出码 `2`
   结束。此步先于一切数据库路径检查，因此**合法 limit 下，非法 URL 优先于
   数据库路径错误报告**。

2. **数据库路径不存在 → 视为空历史** — `healthcheck.py:254-259`
   `os.path.exists(db_path)` 为假时（数据库文件不存在，**连同父目录也不存在**
   一样处理）：普通查询打印 `[]`，摘要打印常量 `NULL_SUMMARY_JSON`
   （`healthcheck.py:69-72`），退出码 `0`。不创建文件、不创建目录。

3. **路径是目录 → 错误** — `healthcheck.py:262-263`
   `os.path.isdir()` 为真时 `die()`，退出码 `2`，stderr 说明“路径是一个目录”。

4. **以只读 URI 打开数据库** — `healthcheck.py:267-271`
   打开串为文件绝对路径的 file URI 加 `?mode=ro`，并以 `uri=True` 连接。
   因此：不执行任何写操作、不会创建 `-wal`/`-journal` 文件；**可读但不可写的
   数据库仍可查询**。打开失败（含读取权限不足）经 `die()` 以退出码 `2` 结束。

5. **判断 `checks` 表是否存在并取数** — `healthcheck.py:274-292`
   - 先查 `sqlite_master` 列出全部表（`healthcheck.py:276-281`）；
   - 没有 `checks` 表（空库或仅有其他表）：`rows = []`，不建表
     （`healthcheck.py:282-284`）；
   - 有 `checks` 表且给了 `--url`：执行 `SELECT_BY_URL_SQL`
     （`healthcheck.py:60-66`），绑定 `(args.url, args.limit)`；
   - 有 `checks` 表且未给 `--url`：执行 `SELECT_SQL`
     （`healthcheck.py:53-58`），绑定 `(args.limit,)`。

   两条 SQL 的关键子句：

   ```sql
   -- SELECT_BY_URL_SQL：先按保存的原始 url 精确等值筛选
   WHERE url = ?
   ORDER BY id DESC
   LIMIT ?

   -- SELECT_SQL：无筛选时跨全部目标
   ORDER BY id DESC
   LIMIT ?
   ```

   即**先按原始 URL 精确筛选，再按 `id` 倒序、最后用 `LIMIT` 截取条数**。
   查询期间发生的任何 `sqlite3.Error`（文件不是有效 SQLite 数据库、`checks`
   表缺少查询所需字段等）统一在 `healthcheck.py:295-297` 经 `die()` 处理，
   退出码 `2`。连接在 `finally` 中关闭（`healthcheck.py:293-294`）。

6. **输出**（单行 JSON，无多余空白，`ensure_ascii=False`）：
   - 摘要分支 `healthcheck.py:299-322`：对第 5 步取得的同一批 `rows` 取
     `row[3]`（`elapsed_ms`）计算 `count / min / max / avg`；`rows` 为空时
     count=0、三个耗时字段为 `null`（`healthcheck.py:312-320`）。退出码 `0`。
   - 记录分支 `healthcheck.py:324-337`：把每行按列序映射为含全部七个字段的
     对象，输出 JSON 数组，退出码 `0`。

---

## 3. 固定样本（复用 `test_recent_summary.py`）

样本由 `build_sample_db()` 写入临时库（`test_recent_summary.py:120-134`），
原始 URL 常量见 `test_recent_summary.py:49-52`，七条记录见
`test_recent_summary.py:58-94`：

- 目标 A：`http://127.0.0.1:8765/health?detail=1`
- 目标 B：`http://127.0.0.1:8765/health?detail=2`（与 A 仅查询值 `detail`
  不同）

| id | 目标 | elapsed_ms | status | http_status | reason | checked_at (UTC) |
|---:|---|---:|---|---:|---|---|
| 1 | A | 90 | success | 200 | ok | 2026-10-02T04:40:**06** |
| 2 | B | 80 | success | 200 | ok | 2026-10-02T04:40:**05** |
| 3 | A | **0** | **failure** | null | connection_error | 2026-10-02T04:40:**04** |
| 4 | B | 70 | success | 200 | ok | 2026-10-02T04:40:**03** |
| 5 | A | 2 | success | 200 | ok | 2026-10-02T04:40:**02** |
| 6 | A | 7 | **failure** | 500 | http_status | 2026-10-02T04:40:**01** |
| 7 | B | 60 | success | 200 | ok | 2026-10-02T04:40:**00** |

两个刻意安排的特征（注释见 `test_recent_summary.py:54-57`）：

- id 1..7 的目标依次为 A、B、A、B、A、A、B；耗时依次为 90、80、0、70、2、
  7、60；A 的 id 3（零耗时）与 id 6（7ms）是 failure，其余 success。
- **`checked_at` 的先后与 id 相反**：id 1 最新、id 7 最早。这用来证明排序只看
  id（见下节）。

---

## 4. 验收示例一：普通查询保留完整记录

命令（与任务约定一致；在会展开 `?` 的 shell 中请自行加引号）：

```sh
python healthcheck.py --db monitor.sqlite recent --url http://127.0.0.1:8765/health?detail=1 --limit 3
```

标准输出（单行 JSON 数组，此处为便于阅读折行，实际无空格无折行）：

```json
[
  {"id":6,"url":"http://127.0.0.1:8765/health?detail=1","checked_at":"2026-10-02T04:40:01.000000+00:00","elapsed_ms":7,"status":"failure","http_status":500,"reason":"http_status"},
  {"id":5,"url":"http://127.0.0.1:8765/health?detail=1","checked_at":"2026-10-02T04:40:02.000000+00:00","elapsed_ms":2,"status":"success","http_status":200,"reason":"ok"},
  {"id":3,"url":"http://127.0.0.1:8765/health?detail=1","checked_at":"2026-10-02T04:40:04.000000+00:00","elapsed_ms":0,"status":"failure","http_status":null,"reason":"connection_error"}
]
```

- 记录 id 顺序为 **6、5、3**：A 共有 id 1、3、5、6 四条，`WHERE url = A`
  精确筛出后 `ORDER BY id DESC` 取前 3，id 1 被 `LIMIT 3` 排除。
  对应断言：`test_recent_summary.py:278-298`
  （`test_same_conditions_without_summary_return_record_arrays`）。
- 每条记录**七个字段完整**：`id/url/checked_at/elapsed_ms/status/http_status/
  reason`（映射见 `healthcheck.py:324-335`）。
- **退出码 0，标准错误为空。**

## 5. 验收示例二：同条件加 `--summary`

```sh
python healthcheck.py --db monitor.sqlite recent --url http://127.0.0.1:8765/health?detail=1 --limit 3 --summary
```

标准输出（单行 JSON 对象）：

```json
{"count":3,"min_elapsed_ms":0,"max_elapsed_ms":7,"avg_elapsed_ms":3.0}
```

- 三条记录耗时为 7（id 6）、2（id 5）、0（id 3），故 count=3、min=0、
  max=7、avg=(7+2+0)/3=**3.0**。对应断言：
  `test_recent_summary.py:245-261`
  （`test_summary_target_a_limit_3_includes_failure_and_zero`）。
- **退出码 0，标准错误为空。**

---

## 6. 排序、筛选与摘要口径（结论与依据）

1. **先精确筛选，再按 id 倒序取条数。** `WHERE url = ?` 在 SQL 内先于
   `ORDER BY`/`LIMIT` 生效（`SELECT_BY_URL_SQL`，`healthcheck.py:60-66`；
   调用处 `healthcheck.py:285-290`）。limit 作用在筛选后的集合上，不是全表。
2. **`checked_at` 不决定排序。** 唯一排序键是 `id DESC`。样本里
   id 6 的 `checked_at`（04:40:01）早于 id 5（04:40:02），id 3（04:40:04）
   又新于 id 5，但输出顺序仍是 6、5、3——按 id 而非时间。全局样本中
   `checked_at` 与 id 反向也是同一目的。
3. **路径或查询参数不同的目标不合并。** 匹配是数据库保存的原始字符串与
   `?` 参数等值比较，不规范化：目标 B（`detail=2`，id 2/4/7）不会混入 A；
   `/health` 与 `/health/`、省略根路径与显式 `/` 也各自独立
   （筛选测试见 `test_recent_url_filter.py:238-281`）。
4. **失败记录与零耗时都参与摘要。** 摘要不看 `status`/`http_status`/`reason`，
   只收集 `elapsed_ms`（`healthcheck.py:299-311`）：id 6（failure，7ms）计入
   max，id 3（failure，0ms）计入 count 且把 min 拉到 0。
5. **平均值不取整。** 平均值是 `sum / count` 的 JSON 数字原值
   （`healthcheck.py:310` 注释“平均值不取整”），9/3 输出 `3.0` 而非 `3`。
6. **摘要统计的就是“同条件 recent 返回的记录子集”。** 普通查询与摘要共用同
   一次取数结果 `rows`（同 URL、同 limit、同 SQL），摘要只是改为对这批行做
   聚合（`healthcheck.py:299-302` 注释明示“对 recent 本会返回的同一批记录”）。
   因此它**不是全历史统计**（limit 生效：示例里 id 1 的 90ms 不在内）、**不是
   时间窗口统计**（没有任何时间条件，排序也与时间无关）、**不是仅成功记录**
   （两条 failure 均计入）。同条件不带 `--summary` 的数组与摘要一一对应，由
   `test_recent_summary.py:278-298` 成对断言。

---

## 7. 空结果与错误的区别

两类情形出口不同：**空结果是正常答案（退出码 0）；错误是参数或数据库不可读
（退出码 2，stdout 为空，原因写 stderr）**。错误出口统一为 `die()`
（`healthcheck.py:75-78`，前缀 `healthcheck: error:`，无 Python 回溯）。

### 7.1 空结果：普通查询 `[]`，摘要 count=0、三耗时字段 null，退出码 0

| 情形 | 处理位置 | 普通输出 | 摘要输出 |
|---|---|---|---|
| 数据库文件不存在（父目录也不存在） | `healthcheck.py:254-259` | `[]` | `NULL_SUMMARY_JSON`（`healthcheck.py:69-72`） |
| 有效数据库但没有 `checks` 表（含仅有其他表） | `healthcheck.py:282-284` | `[]` | count=0、全 null（`healthcheck.py:312-320`） |
| `checks` 表存在但为空表 | 查询返回空 `rows` | `[]` | 同上 |
| 合法目标在库中无匹配记录 | `SELECT_BY_URL_SQL` 返回空 | `[]` | 同上 |

四种空结果均 stderr 为空、退出码 0，且不创建任何文件、目录或表；对应测试
`test_recent_summary.py:305-350`。摘要空对象的实际输出恒为：

```json
{"count":0,"min_elapsed_ms":null,"max_elapsed_ms":null,"avg_elapsed_ms":null}
```

### 7.2 错误：退出码 2、标准输出为空、原因写入标准错误

| 情形 | 触发点 | stderr 要点 |
|---|---|---|
| 非法 URL（非 http、主机非 127.0.0.1、缺端口、片段、userinfo、结构无法解析等） | `validate_target_url()` → `die()`，`healthcheck.py:102-152, 249-250` | 回显非法输入及具体原因（如“仅接受 http 协议”“非法 URL（结构无法解析）”） |
| 非法 limit（0、负数、非整数、`1.5` 等） | argparse 解析期调用 `positive_limit()`，`healthcheck.py:91-99` | argparse usage 信息中含“limit 必须是正整数”；argparse 自身以 2 退出，先于处理函数 |
| `--db` 指向目录 | `healthcheck.py:262-263` | “路径是一个目录，不是 SQLite 数据库文件” |
| 文件存在但不是有效 SQLite 数据库 | 只读查询抛错，`healthcheck.py:295-297` | 如 “file is not a database” |
| 读取权限不足 | 只读打开失败，`healthcheck.py:268-271` | 如 “unable to open database file” |
| `checks` 表缺少查询所需字段 | 查询抛错，`healthcheck.py:295-297` | 如 “no such column: checked_at” |

错误用例对应 `test_recent_summary.py:410-451` 与
`test_recent_url_filter.py:401-457`。

**优先级**：参数解析阶段非法 limit 会最先报错；**在合法 limit（含省略）的前提
下**，`command_recent` 先校验 URL 后碰数据库路径，所以非法 URL 同时搭配目录
路径或不存在的数据库时，一律只报 URL 错误、不报路径错误
（`healthcheck.py:246-250`；测试 `test_recent_summary.py:438-451`、
`test_recent_url_filter.py:422-437`）。

---

## 8. 只读与无副作用保证

`recent` 全流程：

- **不发送任何网络请求**：`command_recent` 不调用 `probe_once()`/网络库；
  摘要查询期间本机服务收不到任何请求，由
  `test_summary_makes_no_network_requests` 守护
  （`test_recent_summary.py:354-385`）。
- **不创建文件或目录**：数据库与父目录均不存在时直接返回空结果
  （`healthcheck.py:254-259`），不调用 `open_database()`（该函数只服务于
  `check`，`healthcheck.py:155-166`）。
- **不创建表、不改动记录**：只读 URI（`?mode=ro`，`healthcheck.py:267`），
  无 `checks` 表时只置空结果（`healthcheck.py:282-284`），绝不执行
  `CREATE TABLE`/`INSERT`。
- **可读但不可写的数据库仍可查询**：只读模式打开，文件权限 `0444` 时查询
  正常返回记录。
- 查询前后目录文件集合与全表内容一致，由各用例中的
  `assert_state_unchanged` / `snapshot_state` 核对
  （`test_recent_summary.py:154-201`）。

---

## 9. 未改变的既有语义（供对照）

以下行为不是本文新增内容，当前版本保持不变：

- **默认 limit 为 5**：省略 `--limit` 时最多返回 5 条
  （`DEFAULT_LIMIT`，`healthcheck.py:25, 366-370`）。固定样本下省略 URL 与
  limit 时取全部目标 id 7、6、5、4、3，摘要为
  `{"count":5,"min_elapsed_ms":0,"max_elapsed_ms":70,"avg_elapsed_ms":27.8}`
  （`test_recent_summary.py:263-274`）。
- **未给 `--url` 时查询全部目标**：走 `SELECT_SQL`（无 WHERE），跨目标按
  id 倒序（`healthcheck.py:291-292`；`test_recent_url_filter.py:285-311`）。
- **`check` 的探测与写入行为不变**：一次 GET、不跟随重定向、结果先落库后
  输出（`probe_once`、`command_check`、`INSERT_SQL`，
  `healthcheck.py:169-240`）；`recent` 能完整读回 `check` 写入的记录
  （`test_recent_summary.py:477-518`）。

---

## 10. 验收依据

1. 第 4、5 节的两个查询示例：id 顺序 6、5、3 的完整记录数组，以及
   `{"count":3,"min_elapsed_ms":0,"max_elapsed_ms":7,"avg_elapsed_ms":3.0}`；
   两者退出码均为 0、stderr 为空。
2. 流程中每一步标注的函数与 SQL/JSON 常量位置（均在当前 `healthcheck.py`
   与 `test_recent_summary.py` 中可直接定位）。
