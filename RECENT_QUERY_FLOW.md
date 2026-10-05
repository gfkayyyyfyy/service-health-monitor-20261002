# `recent` 查询流程说明（单次探测之外的历史读取）

本说明描述 `healthcheck.py` **当前已实现**的 `recent` 子命令流程（含 `--url`、
`--status`、`--reason`、`--since`、`--until` 五个可选筛选），不新增任何行为。
内容从公开命令行参数进入，沿「参数校验 → 数据库只读查询 →（`--since` /
`--until` 任一时间边界生效时）Python 侧时刻过滤与限量 → JSON 输出」讲清每一
步，并标注结论对应的函数或 SQL 常量（行号以当前 `healthcheck.py` 为准）。
`--since` 的完整流程、固定样本与可核对示例见第 6、7 节；`--until` 及双端点
闭合窗口的规则、三记录固定样本与两条完整验收命令见第 6.3、6.4 节与
第 7.4、7.5 节（本地样本准备见第 6.3 节）；错误与边界情形见第 9、10 节。
八种筛选组合（指 url/status/reason 三个库内筛选）此前各由一条独立 SQL 常量
实现，现已重构为单一模板 `SELECT_SQL` 加 `build_recent_query` 动态组装
（行为等价，回归验证见 `test_recent_refactor_regression.py`），本文按重构后的
代码描述；`--since`、`--until` 都不在该组装中新增 SQL 条件，而是在取出符合
其余筛选的记录后于 Python 侧按时刻比较（见第 6 节）。

- 入口函数：`main`（`healthcheck.py:787-790`）→ `command_recent`（`healthcheck.py:546-697`）
- `recent` **不发送网络请求、不创建文件/目录/表、不改动已有记录**；数据库以只读
  URI 打开，可读但不可写的库仍可查询。

> 文中所有命令输出块均为**预期结果**（依据当前实现逐条核对，核对方式见
> `test_recent_since.py`、`test_recent_until.py` 等测试文件），表示在对应
> 样本库上运行时应当得到的 stdout / 退出码；它们不是「本文档生成时已运行」
> 的结论。你可照第 6.1、6.3 节准备样本库并按第 7 节命令自行复现。

---

## 1. 公开参数

命令形式（参数定义见 `build_parser` 中 recent 子解析器，`healthcheck.py:718-782`）：

```sh
python healthcheck.py --db <数据库路径> recent \
    [--url <目标URL>] [--status {success,failure}] \
    [--reason {ok,http_status,connection_error,timeout}] \
    [--since <UTC起始时刻>] [--until <UTC结束时刻>] \
    [--limit <正整数>] [--summary]
```

| 参数 | 是否必填 | 默认 | 约束与语义 | 代码位置 |
|---|---|---|---|---|
| `--db` | 必填（全局） | 无 | SQLite 数据库文件路径 | `healthcheck.py:705` |
| `--url` | 可选 | `None` | 给出时只返回该目标的记录；省略时查询**全部目标** | `healthcheck.py:719-724` |
| `--status` | 可选 | `None`（不筛选） | 给出时只返回该状态的记录；**只接受区分大小写的 `success` / `failure`**，省略时查询全部状态 | `healthcheck.py:725-734` |
| `--reason` | 可选 | `None`（不筛选） | 给出时只返回保存的 reason 与之**精确相等**的记录；**只接受区分大小写的 `ok` / `http_status` / `connection_error` / `timeout`**，省略时查询全部原因；**不从状态码推断** | `healthcheck.py:735-745` |
| `--since` | 可选 | `None`（不筛选） | 给出时只保留 `checked_at` **不早于**该 UTC 时刻（`>=`，相等命中）的记录；格式与日期真实性规则见第 1.4 节；省略时不增加任何时间条件、也不校验记录时间 | `healthcheck.py:746-757` |
| `--until` | 可选 | `None`（不筛选） | 给出时只保留 `checked_at` **不晚于**该 UTC 时刻（`<=`，相等命中）的记录；格式与日期真实性规则与 `--since` 完全相同（见第 1.4、1.5 节）；**单用时不设下界**，与 `--since` 同用构成两端均包含的闭合窗口（起点不得晚于终点，**相等合法**） | `healthcheck.py:758-769` |
| `--limit` | 可选 | `5`（`DEFAULT_LIMIT`，`healthcheck.py:31`） | 正整数；`0`、负数、`1.5`、非数字均被拒绝 | `positive_limit`，`healthcheck.py:151-159` |
| `--summary` | 可选开关 | 关 | 输出这批记录的耗时摘要，而非记录数组 | `healthcheck.py:776-781` |

### 1.1 `--url` 的规则

`--url` 的合法规则与 `check` 完全一致，由 `validate_target_url`
（`healthcheck.py:339-389`）校验：仅 `http` 协议、主机必须是 `127.0.0.1`、
必须显式指定 `1-65535` 端口；允许路径与查询参数；不接受用户信息（userinfo），
原始字符串中只要出现 `#` 即按片段（fragment）拒绝；含空白/控制字符、
结构无法解析、主机或端口非法也一律拒绝。

**注意：校验只用于判定输入是否合法；数据库匹配使用的是命令行上的原始字符串，
不做任何规范化。**

### 1.2 `--status` 的规则

`--status` 由 `validate_status_filter`（`healthcheck.py:162-181`）校验：

- 取值只能是字符串 **`success`** 或 **`failure`**，**区分大小写**：
  `Success`、`FAILURE`、带前后空白（`" failure"`）等都不合法；
- **空字符串**（`--status ""`）不合法；
- **裸 `--status`（命令行上缺少值）** 不合法：argparse 以
  `nargs="?", const=STATUS_FILTER_MISSING`（`healthcheck.py:727-729`）把该
  情形标记为哨兵 `STATUS_FILTER_MISSING`（`healthcheck.py:43`），再由校验函数
  识别并拒绝（与「完全省略 `--status`」的 `None` 严格区分）；
- 以上非法情形一律经 `die`（`healthcheck.py:135-138`）输出 stderr 并以退出码
  `2` 结束，**stdout 为空**，且发生在 URL、reason、since、until 校验与一切
  数据库访问**之前**。

筛选只依据记录的 **`status` 字段**做等值匹配，**不区分失败原因**：
`reason` 为 `http_status` / `connection_error` / `timeout` 的记录在
`--status failure` 下同样命中。

### 1.3 `--reason` 的规则

`--reason` 由 `validate_reason_filter`（`healthcheck.py:184-207`）校验：

- 取值只能是字符串 **`ok`**、**`http_status`**、**`connection_error`**、
  **`timeout`** 四者之一，**区分大小写**：`OK`、`Timeout`、`HTTP_STATUS`、
  带前后空白（`" timeout"`、`"ok "`）等都不合法；
- **空字符串**（`--reason ""`，含纯空白 `"  "`）不合法；
- **裸 `--reason`（命令行上缺少值）** 不合法：argparse 以
  `nargs="?", const=REASON_FILTER_MISSING`（`healthcheck.py:737-739`）把该
  情形标记为哨兵 `REASON_FILTER_MISSING`（`healthcheck.py:50`），再由校验函数
  识别并拒绝（与「完全省略 `--reason`」的 `None` 严格区分）；
- 以上非法情形一律经 `die` 输出 stderr 并以退出码 `2` 结束，**stdout 为空**，
  且发生在 **status、URL 都合法之后、since/until 校验与一切数据库访问之前**——
  拒绝时不会读取、更不会创建数据库。
- 校验在 `command_recent` 中的调用点为 `healthcheck.py:563`。

筛选**只按数据库保存的 `reason` 字段做等值精确匹配，绝不从 `status` 或
`http_status` 推断**：例如 failure 记录的 reason 保存为 `timeout` 时，
`--reason http_status` 与 `--reason connection_error` 都不会命中它，
`--reason timeout` 也不会因为某条 success 记录的 `http_status` 恰好超时而命中
（success 记录的 reason 恒为 `ok`）。与 `--url`、`--status` 同用时三个条件
取交集（AND）；合法组合无匹配时返回空结果，退出码仍为 0。

### 1.4 `--since` 的规则（UTC 起始时刻）

`--since` 由 `validate_since_filter`（`healthcheck.py:262-282`）校验并解析，
格式由常量 `UTC_TIMESTAMP_RE` / `UTC_TIMESTAMP_BODY_RE`
（`healthcheck.py:72-80`）约束，逐时刻解析由 `parse_utc_timestamp`
（`healthcheck.py:210-231`）完成，中文错误原因由 `utc_timestamp_format_problem`
（`healthcheck.py:234-259`）给出。

**合法格式**：`YYYY-MM-DDTHH:MM:SS`，秒后可带**一至六位**小数，并且必须以
**`Z`** 或 **`+00:00`** 结尾。例如：
`2026-10-04T00:00:02Z`、`2026-10-04T00:00:02+00:00`、
`2026-10-04T00:00:02.5Z`、`2026-10-04T00:00:02.000000+00:00` 都合法。

- **日期时间必须真实存在**：正则只校验形状，`datetime(...)` 构造另行把关；
  `2026-02-30T00:00:02Z`、`2026-10-04T25:00:00Z` 等结构合法但不存在的值被拒。
- **`Z` 与 `+00:00` 等价**：两者都表示 UTC，解析为同一时区。
- **显式零小数与省略小数等价**：`.000000` 与不写小数都按整秒处理，
  `2026-10-04T00:00:02Z` 与 `2026-10-04T00:00:02.000000+00:00` 是**同一时刻**。
- 比较按**时刻**进行，不按字符串比较（见第 6.2 节）。

**非法取值一律经 `die` 以退出码 `2` 拒绝，stdout 为空，stderr 以
「`since 参数错误：--since …`」指出 `since` 与具体原因**。各情形与原因关键词：

| 非法情形 | 示例 | stderr 中的原因（`utc_timestamp_format_problem`） |
|---|---|---|
| 缺值（裸 `--since`，命令行未给值） | `--since`（置于末尾） | 「--since 必须提供值……实际缺少值」（哨兵 `SINCE_FILTER_MISSING`，`healthcheck.py:63`） |
| 空值 | `--since ""` | 「值不能为空」 |
| 前后空白 | `--since " 2026-10-04T00:00:02Z "` | 「前后不允许有空白」 |
| 缺时区 | `2026-10-04T00:00:02` | 「缺少时区，必须以 Z 或 +00:00 结尾」 |
| 非 UTC 偏移 | `2026-10-04T00:00:02+08:00` | 「仅接受 UTC 时区（Z 或 +00:00），不接受偏移 +08:00」 |
| 日期不真实 | `2026-02-30T00:00:02Z` | 「日期或时间必须真实存在」 |
| 小数超过六位等形状错误 | `2026-10-04T00:00:02.0000000Z` | 「格式必须为 YYYY-MM-DDTHH:MM:SS（秒后可带一至六位小数），并以 Z 或 +00:00 结尾」 |

该校验在 `command_recent` 中的调用点为 `healthcheck.py:568`，发生在
**status、URL、reason 都合法之后、`--until` 校验与一切数据库访问之前**
（含缺库快路径之前），因此非法 `--since` 与目录/缺库路径同时出现时只报
`since` 错误，不触碰数据库；`--since` 与 `--until` 同时非法时先报 `since`
（见第 1.5、9.1 节）。

### 1.5 `--until` 的规则（UTC 结束时刻）

`--until` 由 `validate_until_filter`（`healthcheck.py:285-306`）校验并解析；
它**复用 `--since` 的全部规则**：同一套 `UTC_TIMESTAMP_RE` 形状约束、同一个
`parse_utc_timestamp` 与 `utc_timestamp_format_problem`（后者文档字符串明确
「`--since` 与 `--until` 共用同一套规则与原因文案」，
`healthcheck.py:237`），仅把错误前缀换成
「`until 参数错误：--until …`」。裸 `--until` 缺值由哨兵
`UNTIL_FILTER_MISSING`（`healthcheck.py:67`）识别，argparse 配置为
`nargs="?", const=UNTIL_FILTER_MISSING`（`healthcheck.py:760-762`）。

语义（与 `--since` 的差别只在比较方向与窗口角色）：

- 保留条件为 `checked_at <= until`（**不晚于**终点，闭区间上界，相等命中）；
- **单用 `--until` 不设下界**：不提供 `--since` 时，早至任意时刻的记录只要
  满足其余筛选都在候选集中；
- 与 `--since` 同用时窗口为 **`since <= checked_at <= until`，两个端点都
  包含**；两端点解析为同一时刻（**包括 `Z` 与 `+00:00` 两种写法**）时窗口
  合法，只含该同一时刻的记录；起点时刻晚于终点才非法（见下）；
- 比较按**时刻**进行，`Z` 与 `+00:00`、显式零小数与省略小数等价
  （同 `parse_utc_timestamp`，见第 1.4、6.2 节）。

非法取值（缺值、空值、前后空白、缺时区、非 UTC 偏移、日期不真实、七位小数等）
与 `--since` 走同一错误通道：退出码 `2`、stdout 空、stderr 为
「`healthcheck: error: until 参数错误：--until <原因>，实际值 <值>`」、无
Python 回溯；逐条实测见第 9.1 节。此外**起点晚于终点**是双端点特有错误：
`--since 2026-10-04T00:00:03Z --until 2026-10-04T00:00:01Z` 在两个参数各自
校验通过后，由 `command_recent` 的时刻比较
（`healthcheck.py:573-582`）以退出码 `2` 拒绝，stderr 为
「`时间范围参数错误：--since 给出的起始时刻 … 晚于 --until 给出的结束时刻 …，
起点不得晚于终点（二者相等时为只含同一时刻的合法窗口）`」，stdout 为空。

该校验在 `command_recent` 中的调用点为 `healthcheck.py:572`，紧随
`--since`（`:568`）之后、窗口次序检查（`:573-582`）之前，整体仍先于
缺库分支（`:589`）与一切数据库访问；因此**参数错误的固定报告顺序为
status → URL → reason → since → until（含窗口次序）→ 数据库路径错误**。

---

## 2. 端到端流程

`main` 解析参数后调用 `args.handler`，对 `recent` 即 `command_recent`
（`healthcheck.py:546-697`）。步骤严格按以下顺序发生：

1. **先校验 `--status`**（`healthcheck.py:552`）。
   提供了 `--status` 时先调用 `validate_status_filter`；非法值（含空字符串、
   裸 `--status` 缺值、大小写不符等）立即经 `die` 以退出码 `2` 结束。
   这一步先于 URL、reason、since、until 校验与一切数据库路径判断，因此
   **status 非法时优先报 status 错误**，即使同时给出非法 URL、非法 reason、
   非法 since/until 或不存在/目录形式的数据库路径。

2. **再校验筛选 URL**（`healthcheck.py:557-558`）。
   提供了 `--url` 时调用 `validate_target_url(args.url)`；非法 URL 立即经
   `die` 以退出码 `2` 结束。status 合法后，**非法 URL 仍优先于 reason、
   since/until 与数据库路径错误**。仅做校验，匹配时仍使用原始字符串。

3. **再校验 `--reason`**（`healthcheck.py:563`）。
   提供了 `--reason` 时调用 `validate_reason_filter`；非法值（含空字符串、
   裸 `--reason` 缺值、大小写不符、前后空白等）立即经 `die` 以退出码 `2`
   结束。status 与 URL 都合法后，**非法 reason 仍优先于 since/until 与数据库
   路径错误**（目录、缺库等都不会再被报告），且拒绝发生在数据库访问之前。

4. **再校验 `--since`**（`healthcheck.py:568`）。
   提供了 `--since` 时调用 `validate_since_filter`，返回一个 UTC aware
   `datetime`（`None` 表示省略、不筛选）；缺值、空值、前后空白、缺时区、
   非 UTC 偏移、非法日期时间立即经 `die` 以退出码 `2` 结束。

5. **再校验 `--until` 并检查窗口次序**（`healthcheck.py:572-582`）。
   提供了 `--until` 时调用 `validate_until_filter`，规则与错误通道同
   `--since`（见第 1.5 节），返回 UTC aware `datetime`（`None` 表示不设上界）。
   两个边界都给出时，再比较解析后的时刻：**起点晚于终点**立即经 `die` 以
   退出码 `2` 结束（相等合法，不报）；比较的是时刻，故
   `--since …02Z --until …02.000000+00:00` 这种两种写法的相等组合合法。
   **处理函数的校验顺序固定为 status → URL → reason → since → until（含
   窗口次序），之后才访问数据库**；因此报告优先级为
   **status ＞ URL ＞ reason ＞ since ＞ until（含窗口次序）＞ 数据库路径
   错误**；`--since` 与 `--until` 同时非法时只报 `since`
   （`test_invalid_since_reported_before_invalid_until`）。

6. **数据库路径不存在 → 视为空历史**（`healthcheck.py:589-594`）。
   `os.path.exists` 为假时（文件不存在，**连父目录一起不存在也相同**），
   以空记录集调用与正常查询相同的呈现入口 `render_recent_result`：
   普通查询输出 `[]`，摘要输出 count 0 且三个耗时字段为 null，退出码 `0`。此分支不打开 sqlite，不创建
   任何东西——即使给了合法 `--since`/`--until` 也一样。

7. **路径是目录 → 错误**（`healthcheck.py:597-598`）。
   经 `die` 报「路径是一个目录，不是 SQLite 数据库文件」，退出码 `2`。

8. **以只读模式打开数据库**（`healthcheck.py:602-606`）。
   打开的是文件绝对路径的 file URI 并附 `?mode=ro`：
   `pathlib.Path(...).as_uri() + "?mode=ro"`。因此：
   - 不执行 `CREATE TABLE`，不会创建或修改数据库文件，也不产生 `-wal`/`-journal`；
   - 权限为「可读不可写」的文件照样可以查询；
   - 无法打开（如读取权限不足）时经 `die` 结束，退出码 `2`。
   注意：`check` 使用的 `open_database`（`healthcheck.py:392-408`，会建库建表）
   **recent 从不调用**。

9. **识别历史表并执行查询**（`healthcheck.py:611-651`）。
   先查 `sqlite_master`，表名按**大小写不敏感**识别历史表：`CHECKS`、
   `Checks` 等与 `checks` 是同一历史表（SQLite 标识符本身大小写不敏感，
   这类异写表至多存在一个），查询时使用库中保存的实际表名（加引号）。
   有效库中没有历史表（空库或仅有其他表）时，`rows = []`，不报错、不建表。
   识别到历史表后，先用 `ensure_checks_columns`（`healthcheck.py:411-423`，
   与 `check` 探测前的结构校验同一函数）核对七个所需字段，**缺任意一个即
   经 `die` 报「checks 表缺少字段: …」以退出码 `2` 结束，不再误判为空历史**；
   字段名比较沿用 SQLite 的大小写不敏感语义，列顺序不同或存在额外列均可。
   字段齐全后，由 `build_recent_query`（`healthcheck.py:519-543`）按三个库内
   筛选的组合统一组装查询：模板只有一条 `SELECT_SQL`
   （`healthcheck.py:112-118`），`{where}` 按当前组合由
   `RECENT_FILTER_CLAUSES`（`healthcheck.py:122-126`）中对应的条件片段
   （`url = ?` / `status = ?` / `reason = ?`，按 url → status → reason 的
   固定顺序以 AND 拼接）生成，无筛选时为空字符串；选择列顺序都是
   `id, url, checked_at, elapsed_ms, status, http_status, reason`，
   且一律 `ORDER BY id DESC`。是否在 SQL 内限量取决于是否有任一时间边界
   生效（`time_filter_active = since 给了或 until 给了`，
   `healthcheck.py:585`）：

   | `--url` | `--status` | `--reason` | WHERE 子句 | 省略 `--since` 与 `--until`：绑定参数 | 任一时间边界生效：绑定参数 |
   |---|---|---|---|---|---|
   | 省略 | 省略 | 省略 | （无） | `(limit,)` | `()`（SQL 无 LIMIT） |
   | 给出 | 省略 | 省略 | `WHERE url = ?` | `(url, limit)` | `(url,)` |
   | 省略 | 给出 | 省略 | `WHERE status = ?` | `(status, limit)` | `(status,)` |
   | 省略 | 省略 | 给出 | `WHERE reason = ?` | `(reason, limit)` | `(reason,)` |
   | 给出 | 给出 | 省略 | `WHERE url = ? AND status = ?` | `(url, status, limit)` | `(url, status)` |
   | 给出 | 省略 | 给出 | `WHERE url = ? AND reason = ?` | `(url, reason, limit)` | `(url, reason)` |
   | 省略 | 给出 | 给出 | `WHERE status = ? AND reason = ?` | `(status, reason, limit)` | `(status, reason)` |
   | 给出 | 给出 | 给出 | `WHERE url = ? AND status = ? AND reason = ?` | `(url, status, reason, limit)` | `(url, status, reason)` |

   - **两个时间边界都省略**（`sql_limit=True`）：SQL 为
     `… ORDER BY id DESC LIMIT ?`，库内一次完成「按全部给定条件筛选 →
     按 id 倒序 → 取前 limit 条」（`healthcheck.py:645-646`）。
   - **`--since` / `--until` 任一被给出**（`time_filter_active` 为真，
     `sql_limit=False`，`healthcheck.py:643, 647-651`）：SQL **不含 LIMIT**，
     先取出符合 url/status/reason 的**全部**记录（已按 id 倒序），再交由
     `filter_rows_by_since`（`healthcheck.py:309-336`，函数名沿用 `--since`
     初版命名，现已同时处理 `--until`）在 Python 侧逐条解析 `checked_at`、
     按时刻保留落在窗口内者（下界 `>= since` 省略则不设、上界
     `<= until` 省略则不设），**最后才** `kept[:limit]` 截取。这样做是因为
     时间条件按时刻比较、且要对全部候选记录校验时间格式（见第 6 节），不能
     在 SQL 内用字符串比较或提前限量。

   无论是否给出时间边界，语义都统一为**先用全部给定条件筛选，再限量**：
   limit 永远作用在筛选之后，不会先截断再筛选；排序键只有 id。`reason`
   条件是 `WHERE reason = ?`，与 `http_status`、`status` 列无关，不做任何
   推断或重映射。

   查询期 sqlite 错误（文件不是有效 SQLite 库、读取失败等）
   由 `except sqlite3.Error` 捕获（`healthcheck.py:654-657`），经 `die` 以
   退出码 `2` 结束。

10. **输出 JSON**（单行，紧凑分隔，`ensure_ascii=False`，末尾一个换行）：
    - `--summary`：见第 5、8 节（`healthcheck.py:659-682`）；
    - 普通查询：把每行按七字段映射为记录对象数组
      （`healthcheck.py:684-696`），`json.dumps(..., separators=(",", ":"))`
      输出（`healthcheck.py:696`）。无记录时输出 `[]`。

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

预期输出（单行 JSON；退出码 `0`，stderr 为空）：

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
  （`healthcheck.py:112-118, 122-126`）。
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

预期输出（单行 JSON；退出码 `0`，stderr 为空）：

```json
{"count":3,"min_elapsed_ms":0,"max_elapsed_ms":7,"avg_elapsed_ms":3.0}
```

**摘要统计口径确认**（实现：`healthcheck.py:659-682`）：摘要统计的就是
**同条件 `recent`（不带 `--summary`）会返回的同一记录子集**——同样的筛选
条件（含 `--reason`、`--since`、`--until`）、同样按 id 倒序、同样的 limit
截取；代码对这批 `rows` 直接取 `row[3]`（即 `elapsed_ms`）计算，并不另外
发起任何查询。
因此它：

- **不是全历史统计**：id 1（90 ms）因 `LIMIT 3` 被排除，不计入；
- **不是时间窗口统计（本例未给 `--since`/`--until`）**：没有时间条件时，
  `checked_at` 只作为输出字段；给出任一时间边界时则只统计窗口过滤后的同一
  子集（见第 7.2、7.5 节）；
- **不是仅成功记录**：id 6（failure，7 ms）与 id 3（failure，0 ms）都计入；
- **零耗时参与统计**：`min_elapsed_ms` 为 0（id 3），count 仍为 3；
- **平均值不取整**：`(7 + 2 + 0) / 3 = 3.0`，以 JSON 数字原样输出
  （`sum(...) / count` 的浮点除法，`healthcheck.py:670`），故序列化为 `3.0`
  而非 `3`；max 为 7（id 6）。

无记录时（缺库、无历史表、空表、筛选无匹配——含时间窗口过滤后无保留），
摘要为 `count: 0` 且三个耗时字段为 `null`：缺库快路径与其余路径都以空
记录集走同一呈现入口 `render_recent_result`（由 `build_elapsed_summary`
构造空摘要），输出逐字节一致，退出码均为 `0`。

---

## 6. `--since` / `--until` 时间窗口筛选：固定样本与判定规则

### 6.1 固定样本四（复用 `test_recent_since.py`，4 条）

该样本专门验收 `--since`，共 4 条记录，**全部为 failure / timeout、
`http_status` 为 null**，分属两个仅路径不同的目标：

- 目标 A：`http://127.0.0.1:8765/health`
- 目标 B：`http://127.0.0.1:8765/ready`

| id | 目标 | elapsed_ms | status | http_status | reason | checked_at（数据库中保存的原始字符串） |
|----|------|-----------|--------|-------------|--------|------------------|
| 1 | A | **0** | failure | null | timeout | `2026-10-04T00:00:02Z` |
| 2 | A | **10** | failure | null | timeout | `2026-10-04T00:00:02.000000+00:00` |
| 3 | B | 20 | failure | null | timeout | `2026-10-04T00:00:04Z` |
| 4 | A | 30 | failure | null | timeout | `2026-10-04T00:00:01Z` |

即 id 1..4 依次为 A/0 ms、A/10 ms、B/20 ms、A/30 ms。关键安排：

- **id 1 与 id 2 是同一时刻 `2026-10-04T00:00:02` 的两种字符串写法**
  （一个以 `Z` 结尾且省略小数，一个以 `+00:00` 结尾且显式六位零小数），
  用来区分「按时刻比较」与「按字符串比较」；
- id 4（A）早一秒（`00:00:01`），id 3（B）晚两秒（`00:00:04`），
  用来验证时刻边界与 url 交集。

### 6.2 一条记录为何被保留、排除或报错

`--since` / `--until` 任一边界生效时，对 SQL 取出的每条候选记录（已满足
url/status/reason、已按 id 倒序），`filter_rows_by_since`
（`healthcheck.py:309-336`；函数名沿用 `--since` 初版命名，现已同时处理
`--until`）依次做两件事：

1. **解析并校验 `checked_at`**：`parse_utc_timestamp(row[2])`
   （`healthcheck.py:324`）。记录的时间串必须与 `--since`/`--until` 遵守
   同一套严格 UTC 格式（`UTC_TIMESTAMP_RE`）。解析失败（如
   `"not-a-timestamp"`）立即 `die`：退出码 `2`、stdout 空、stderr 为
   「记录 `id=<该记录id>` 的 checked_at 不符合 UTC 时间格式……」
   并回显原始串（`healthcheck.py:325-330`）。
   **只要任一时间边界生效，校验就针对全部符合其余筛选的记录，发生在时间
   比较与 `kept[:limit]` 截取之前**（`for row in rows` 遍历的是未截断的
   全集）——所以坏 `checked_at` 即使按 id 倒序排在 limit 之外、或即使合法也
   落在窗口之外，仍会被发现并拒绝；被 url/status/reason 排除的记录根本不在
   `rows` 中，不会被触碰。详见第 9.2 节。
2. **按时刻比较决定保留**：给了 `--since` 且 `checked_at < since` 则跳过
   （`healthcheck.py:331-332`）；给了 `--until` 且 `checked_at > until` 则
   跳过（`healthcheck.py:333-334`）；两个跳过都未命中才保留
   （`kept.append(row)`，`healthcheck.py:335`）。即保留条件为
   **`since <= checked_at <= until`**，省略的一侧不设限；两端比较都是
   闭区间（下界 `>=`、上界 `<=`），边界相等命中。

由此得到本样本在起点 `2026-10-04T00:00:02Z`（配合
`--url A --status failure --reason timeout`）下的逐条结论：

| id | 是否进入候选（url/status/reason） | 解析后的时刻 | 与起点比较 | 结论 |
|----|---|---|---|---|
| 1 | 是（A/failure/timeout） | 00:00:02 | `==` 起点 | **保留（相等命中，边界含）** |
| 2 | 是（A/failure/timeout） | 00:00:02 | `==` 起点 | **保留（相等命中，边界含）** |
| 3 | 否（url 为 B） | — | — | 被 `WHERE url = ?` 排除，不参与时间校验 |
| 4 | 是（A/failure/timeout） | 00:00:01 | `<` 起点 | 时刻更早，排除 |

- **起点相等为何命中**：判定是 `checked_at >= since`（闭区间下界），不是
  严格大于。id 1、2 的时刻恰好等于起点，故保留。把起点推进一微秒到
  `2026-10-04T00:00:02.000001Z` 后，id 1、2 都早于它，结果即为空
  （普通 `[]`、摘要 count 0 三 null，退出码 0），证明相等边界确实是 `>=`。
- **`Z` 与 `+00:00` 为何等价**：二者都表示 UTC 偏移 0，`parse_utc_timestamp`
  统一构造为带 `tzinfo=timezone.utc` 的 aware datetime（`healthcheck.py:224-228`），
  比较的是同一时标下的时刻，后缀写法不影响结果。
- **显式零小数与省略小数为何等价**：小数缺省按 `0` 处理
  （`int((fraction or "0").ljust(6, "0"))`，`healthcheck.py:222`），
  故 `00:00:02` 与 `00:00:02.000000` 的微秒都是 0，是同一时刻。
- **输出为何保留原始 `checked_at`**：比较在解析后的 datetime 之间进行，但
  输出映射直接取记录的 `row[2]`（`healthcheck.py:688`），不做任何归一化。
  因此同刻的 id 2 输出 `...02.000000+00:00`、id 1 输出 `...02Z`，原样保留
  数据库里的写法，不会被统一改写成 `Z` 或 `+00:00`。
- **先筛选、后限量；排序依据 id；摘要只统计最终返回的记录**：
  SQL 先按 url/status/reason 过滤并 `ORDER BY id DESC`，Python 侧完成时刻
  保留后才 `kept[:limit]`（`healthcheck.py:336`），因此排序键始终是 id、
  limit 截断的是窗口过滤后的列表；摘要对截断后的同一 `rows` 取 `elapsed_ms`
  统计（`healthcheck.py:662`）。本例保留 id 2、1（倒序），limit 2 不额外
  截断；摘要只统计这两条的 10 ms 与 0 ms（见第 7.1、7.2 节）。

### 6.3 固定样本五（复用 `test_recent_until.py`，3 条验收样本）与本地准备方法

该样本专门验收 `--until`（单用及其与 `--since` 的组合），共 **3 条记录，
全部属于同一目标** `http://127.0.0.1:8765/`；id、时间、状态与
`test_recent_until.py` 顶部的 `REC1/REC2/REC3`（`ALL_RECORDS`）完全一致，
耗时依次为 0、10、20 毫秒：

| id | url | elapsed_ms | status | http_status | reason | checked_at（数据库中保存的原始字符串） |
|----|-----|-----------|--------|-------------|--------|------------------|
| 1 | `http://127.0.0.1:8765/` | **0** | success | 200 | ok | `2026-10-04T00:00:01Z` |
| 2 | `http://127.0.0.1:8765/` | **10** | failure | null | timeout | `2026-10-04T00:00:02.000000+00:00` |
| 3 | `http://127.0.0.1:8765/` | **20** | success | 200 | ok | `2026-10-04T00:00:03Z` |

关键安排：

- **id 1 与 id 2 是相邻两秒**，且 id 2 用 `+00:00` + 六位零小数写法、id 1 用
  `Z` 省略小数写法；两条**一成功一失败**（id 2 为 failure/timeout、
  `http_status` 为 null），且 id 1 **耗时为 0**——用来核对失败记录与零耗时
  都正常返回并计入摘要；
- 三条时刻严格递增（01、02、03 秒），故以 id 1、id 2 的时刻作为闭合窗口的
  起点 `2026-10-04T00:00:01Z`、终点 `2026-10-04T00:00:02Z` 时恰好截出
  id 2、1，id 3 被上界排除（第 7.4、7.5 节）。

**可复现的本地样本准备**（只用 Python 3 标准库，直接建库不发任何网络请求；
与测试夹具 `build_sample_db` 写入的内容等价）。在项目目录执行：

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
        (1, "http://127.0.0.1:8765/", "2026-10-04T00:00:01Z",
         0, "success", 200, "ok"),
        (2, "http://127.0.0.1:8765/", "2026-10-04T00:00:02.000000+00:00",
         10, "failure", None, "timeout"),
        (3, "http://127.0.0.1:8765/", "2026-10-04T00:00:03Z",
         20, "success", 200, "ok"),
    ],
)
conn.commit()
conn.close()
EOF
```

表结构即产品代码的 `CREATE_TABLE_SQL`（`healthcheck.py:82-93`），测试文件
`test_recent_until.py` 中的 `SCHEMA_SQL` 与其逐字一致；准备完成后即可在同一
目录照第 7.4、7.5 节命令复现本文全部输出。**本文给出的所有输出都是该样本上的
预期结果**（逐条对应测试：主场景两条命令分别为
`test_inclusive_window_limit_returns_id_2_1_full_records` 与
`test_inclusive_window_summary_count_min_max_avg`），不是某次实际运行的转存。

### 6.4 闭合窗口下三条记录的逐条结论（起点 00:00:01Z、终点 00:00:02Z）

第 7.4、7.5 节的两条命令都带
`--url http://127.0.0.1:8765/ --since 2026-10-04T00:00:01Z
--until 2026-10-04T00:00:02Z --limit 2`。SQL 不带 LIMIT，先按 `url = ?`
取出同目标的全部记录（id 倒序 3、2、1），再由 `filter_rows_by_since`
逐条解析并按窗口判定：

| id | 解析后的时刻 | 与起点 `00:00:01` 比 | 与终点 `00:00:02` 比 | 结论 |
|----|---|---|---|---|
| 3 | 00:00:03 | `>=` 起点 | **`>` 终点** | 晚于上界，排除（`healthcheck.py:333-334`） |
| 2 | 00:00:02 | `>=` 起点 | `==` 终点 | **保留（上端点相等命中，`<=` 含边界）** |
| 1 | 00:00:01 | `==` 起点 | `<=` 终点 | **保留（下端点相等命中，`>=` 含边界）** |

保留结果沿用 SQL 的 id 倒序为 **id 2、id 1**，`kept[:2]` 不额外截断；输出里
id 2 保留 `...02.000000+00:00`、id 1 保留 `...01Z` 的原始写法。

- **`--until` 单用不设下界**：去掉该命令的 `--since` 后，只保留
  `checked_at <= 00:00:02` 一条判定，结果仍是 id 2、1
  （`test_until_alone_has_no_lower_bound`）；把终点放到
  `2026-10-04T00:00:10Z` 则三条全部保留，摘要 count 3、min 0、max 20、
  avg 10.0（`test_until_alone_summary_counts_zero_elapsed`，证明零耗时计入）。
- **双端点均包含、相等合法**：`--until 2026-10-04T00:00:03Z` 时 id 3 时刻
  恰等于终点仍返回 [3, 2, 1]（`test_upper_endpoint_inclusive`）；
  `--since 2026-10-04T00:00:02Z --until 2026-10-04T00:00:02.000000+00:00`
  两端点写法不同但时刻相等，窗口合法，只返回 id 2
  （`test_equal_since_and_until_is_valid`）。
- **`Z` 与 `+00:00` 按时刻等价**：`--until 2026-10-04T00:00:02Z` 与
  `--until 2026-10-04T00:00:02.000000+00:00` 输出完全相同
  （`test_until_equivalent_forms_compare_as_same_instant`）。
- **全部筛选取交集后才限量**：窗口与 `--url`（以及同用时的 status/reason）
  先在 SQL 与 Python 两阶段取交集，limit 只截取交集结果；同参数省略
  `--until` 时 id 3 不再被上界排除，limit 2 按 id 倒序得到 [3, 2]
  （`test_without_until_results_unchanged`），对照可见上界在限量**之前**
  生效。摘要统计的始终是窗口交集、限量之后的同一批记录（第 7.5 节）。

---

## 7. 验收示例：`--since` / `--until` 与 url/status/reason 组合

7.1–7.3 节（验收示例五）的样本库为第 6.1 节的 4 条记录
（`monitor.sqlite`）。目标 A 为
`http://127.0.0.1:8765/health`，起点 `2026-10-04T00:00:02Z`。

### 7.1 验收示例五（普通查询）：依次返回 id 2、1

```sh
python healthcheck.py --db monitor.sqlite recent \
    --url http://127.0.0.1:8765/health \
    --status failure --reason timeout \
    --since 2026-10-04T00:00:02Z --limit 2
```

预期输出（单行 JSON；退出码 `0`，stderr 为空）：

```json
[{"id":2,"url":"http://127.0.0.1:8765/health","checked_at":"2026-10-04T00:00:02.000000+00:00","elapsed_ms":10,"status":"failure","http_status":null,"reason":"timeout"},{"id":1,"url":"http://127.0.0.1:8765/health","checked_at":"2026-10-04T00:00:02Z","elapsed_ms":0,"status":"failure","http_status":null,"reason":"timeout"}]
```

- SQL 组合为 `WHERE url = ? AND status = ? AND reason = ?`（**不带 LIMIT**，
  `build_recent_query(..., sql_limit=False)`，`healthcheck.py:636-648`），
  取出 A 的 failure/timeout：id 4、2、1（id 倒序）；id 3 属 B 被 url 排除。
- `filter_rows_by_since` 解析三条的 `checked_at` 并与起点比较：
  id 4（00:00:01）早于起点被排除；id 2、id 1 都等于起点被保留
  （`>=`，见第 6.2 节）。
- 保留结果按原 id 倒序为 **id 2、id 1**，`kept[:2]` 后不变。
  两条同刻但字符串写法不同，输出各自保留原始 `checked_at`。
- 把起点换成等价写法 `2026-10-04T00:00:02.000000+00:00`，预期输出**完全相同**
  （`Z`/`+00:00`、零小数等价，回归用例
  `test_since_equivalent_offset_form_same_result`）。

### 7.2 验收示例五（摘要）：同一条件加 `--summary`，count 2，min 0 / max 10 / avg 5.0

```sh
python healthcheck.py --db monitor.sqlite recent \
    --url http://127.0.0.1:8765/health \
    --status failure --reason timeout \
    --since 2026-10-04T00:00:02Z --limit 2 --summary
```

预期输出（单行 JSON；退出码 `0`，stderr 为空）：

```json
{"count":2,"min_elapsed_ms":0,"max_elapsed_ms":10,"avg_elapsed_ms":5.0}
```

摘要只统计第 7.1 节普通查询**最终返回的同一子集 id 2、1**（时刻过滤并限量
之后）：耗时为 10、0，故 `count=2`、`min=0`（id 1 的零耗时计入）、
`max=10`、`avg=(10+0)/2=5.0`（浮点除法不取整，序列化为 `5.0`）。
被时刻排除的 id 4（30 ms）与被 url 排除的 id 3（20 ms）都**不**计入。
起点用 `.000000+00:00` 等价写法时摘要相同
（`test_since_equivalent_offset_form_summary_same`）。

### 7.3 边界推进一微秒：无匹配

起点改为 `2026-10-04T00:00:02.000001Z`（其余参数不变）：id 1、2 都早于它，
id 4 更早，A 中无记录保留。

- 普通查询预期输出 `[]`（末尾一个换行）；
- 摘要预期输出
  `{"count":0,"min_elapsed_ms":null,"max_elapsed_ms":null,"avg_elapsed_ms":null}`；
- 两者退出码均为 `0`、stderr 为空
  （`test_since_one_microsecond_later_plain_output_empty`、
  `test_since_one_microsecond_later_summary_nulls`）。

### 7.4 验收示例六：`--since` + `--until` 闭合窗口（普通查询返回 id 2、1）

样本库为第 6.3 节的 3 条记录（`monitor.sqlite`，可用该节的脚本本地准备）。
窗口起点 `2026-10-04T00:00:01Z`、终点 `2026-10-04T00:00:02Z`，limit 为 2：

```sh
python healthcheck.py --db monitor.sqlite recent \
    --url http://127.0.0.1:8765/ \
    --since 2026-10-04T00:00:01Z --until 2026-10-04T00:00:02Z --limit 2
```

预期输出（单行 JSON；退出码 `0`，stderr 为空）：

```json
[{"id":2,"url":"http://127.0.0.1:8765/","checked_at":"2026-10-04T00:00:02.000000+00:00","elapsed_ms":10,"status":"failure","http_status":null,"reason":"timeout"},{"id":1,"url":"http://127.0.0.1:8765/","checked_at":"2026-10-04T00:00:01Z","elapsed_ms":0,"status":"success","http_status":200,"reason":"ok"}]
```

要点（逐条判定见表 6.4）：

- 每个对象都是完整**七字段数组**（`id/url/checked_at/elapsed_ms/status/
  http_status/reason`），id 顺序为 **2、1**（最终按 id 倒序，与时刻先后
  无关）；`checked_at` 原样返回——id 2 是数据库里的
  `...02.000000+00:00`、id 1 是 `...01Z`，不做归一化。
- **失败记录正常返回**：id 2 是 failure/timeout、`http_status` 为 null；
  **零耗时正常返回**：id 1 的 `elapsed_ms` 为 0。
- id 3（00:00:03，20 ms）虽满足 `--url`，但时刻**晚于终点**被
  `checked_at > until` 跳过；两个端点都包含，故恰在终点的 id 2 与恰在起点
  的 id 1 都保留。limit 2 作用在窗口交集之后，此处不额外截断。

### 7.5 验收示例七：同一窗口加 `--summary`：count 2，min 0 / max 10 / avg 5.0

```sh
python healthcheck.py --db monitor.sqlite recent \
    --url http://127.0.0.1:8765/ \
    --since 2026-10-04T00:00:01Z --until 2026-10-04T00:00:02Z \
    --limit 2 --summary
```

预期输出（单行 JSON；退出码 `0`，stderr 为空）：

```json
{"count":2,"min_elapsed_ms":0,"max_elapsed_ms":10,"avg_elapsed_ms":5.0}
```

摘要统计的是第 7.4 节普通查询**最终返回的同一批记录 id 2、1**（窗口取交集、
按 id 倒序、limit 截取之后，`healthcheck.py:659-682`）：耗时 10、0，故
`count=2`、`min=0`（**id 1 的零耗时计入**）、`max=10`（**failure 记录 id 2
计入**）、`avg=(10+0)/2=5.0`（浮点除法不取整，序列化为 `5.0`）。被上界
排除的 id 3（20 ms）不参与统计。窗口内无匹配（或数据库不存在）时，摘要为
`{"count":0,"min_elapsed_ms":null,"max_elapsed_ms":null,"avg_elapsed_ms":null}`，
退出码仍为 `0`（见第 9.3、10.1 节）。

---

## 8. 固定样本二、三与 `--status` / `--reason` 验收示例

下面两节样本与示例不使用 `--since` / `--until`，用于核对 url/status/reason
三个库内筛选本身的行为；它们与第 6、7 节的时间窗口筛选正交（同用时取交集，
流程见第 2 节步骤 9）。

### 8.1 固定样本二（`test_recent_status_filter.py`，5 条验收样本）

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

### 8.2 验收示例三：`--url` 与 `--status` 组合（普通查询 + 摘要）

```sh
python healthcheck.py --db monitor.sqlite recent \
    --url http://127.0.0.1:8765/health?detail=1 --status failure --limit 2
```

预期输出（退出码 `0`，stderr 为空）：

```json
[{"id":4,"url":"http://127.0.0.1:8765/health?detail=1","checked_at":"2026-10-04T00:00:04.000000+00:00","elapsed_ms":3,"status":"failure","http_status":null,"reason":"timeout"},{"id":1,"url":"http://127.0.0.1:8765/health?detail=1","checked_at":"2026-10-04T00:00:01.000000+00:00","elapsed_ms":0,"status":"failure","http_status":null,"reason":"connection_error"}]
```

要点：

- 命中条件是 **url 等于 A 且 status 等于 failure**：
  `WHERE url = ? AND status = ?`（由 `build_recent_query` 按组合组装，
  `healthcheck.py:519-543`，执行点 `636-646`）；
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

预期输出：

```json
{"count":2,"min_elapsed_ms":0,"max_elapsed_ms":3,"avg_elapsed_ms":1.5}
```

统计口径与第 5 节完全相同：只统计这次普通查询本会返回的 id 4、1 两条；
id 1 的零耗时参与统计（min 为 0，count 仍为 2）；均值 `(3 + 0) / 2 = 1.5`
不取整。

补充：只给 `--status failure`（省略 `--url`，默认 limit 5）时跨全部目标命中
id 4、3、1（B 的 id 3 也在内），对应 `WHERE status = ?` 组合；
`--status success` 则命中 id 5、2。

### 8.3 固定样本三（`test_recent_reason_filter.py`，5 条 reason 验收样本）

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

### 8.4 验收示例四：`--url` 与 `--reason` 组合（普通查询 + 摘要）

```sh
python healthcheck.py --db monitor.sqlite recent \
    --url http://127.0.0.1:8765/health --reason timeout --limit 2
```

预期输出（退出码 `0`，stderr 为空）：

```json
[{"id":3,"url":"http://127.0.0.1:8765/health","checked_at":"2026-10-04T00:00:03.000000+00:00","elapsed_ms":10,"status":"failure","http_status":null,"reason":"timeout"},{"id":1,"url":"http://127.0.0.1:8765/health","checked_at":"2026-10-04T00:00:01.000000+00:00","elapsed_ms":0,"status":"failure","http_status":null,"reason":"timeout"}]
```

要点：

- 命中条件是 **url 等于 A 且保存的 reason 等于 timeout**：
  `WHERE url = ? AND reason = ?`（由 `build_recent_query` 按组合组装，
  `healthcheck.py:519-543`，执行点 `636-646`）；
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

预期输出：

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

## 9. `--since` / `--until` 的错误与边界情形

### 9.1 非法时间参数：退出码 2、stdout 空、stderr 指出参数与原因

所有第 1.4 节表中的非法取值（缺值、空值、前后空白、缺时区、非 UTC 偏移、
非法日期、七位小数等）对 `--since` 与 `--until` 行为一致：退出码 `2`、
**stdout 完全为空**、stderr 以 `healthcheck: error:` 开头，按参数分别写作
`since 参数错误：--since …` 或 `until 参数错误：--until …`，说明具体原因、
回显实际值，且不含 Python 回溯。`--since` 的逐条用例见
`test_recent_since.py::test_since_bare_missing_value_rejected` 与
`test_since_invalid_values_rejected`（`INVALID_SINCE_CASES`）；`--until` 对应
`test_recent_until.py::test_until_bare_missing_value_rejected` 与
`test_until_invalid_values_rejected`（`INVALID_UNTIL_CASES`，取值与原因关键词
一一对应）。

`--until` 实测 stderr 示例（在第 6.3 节样本库上逐条运行得到；`--since` 只把
前缀中的 `until` 换成 `since`，原因文案相同）：

| 非法情形 | 取值 | stderr（前缀省略，单行） |
|---|---|---|
| 空值 | `--until ""` | `until 参数错误：--until 值不能为空，实际值 ''` |
| 前后空白 | `--until " 2026-10-04T00:00:02Z "` | `until 参数错误：--until 前后不允许有空白，实际值 ' 2026-10-04T00:00:02Z  '` |
| 缺时区 | `--until 2026-10-04T00:00:02` | `until 参数错误：--until 缺少时区，必须以 Z 或 +00:00 结尾，实际值 '2026-10-04T00:00:02'` |
| 非 UTC 偏移 | `--until 2026-10-04T00:00:02+08:00` | `until 参数错误：--until 仅接受 UTC 时区（Z 或 +00:00），不接受偏移 +08:00，实际值 '2026-10-04T00:00:02+08:00'` |
| 日期不真实 | `--until 2026-02-30T00:00:02Z` | `until 参数错误：--until 日期或时间必须真实存在，实际值 '2026-02-30T00:00:02Z'` |
| 七位小数 | `--until 2026-10-04T00:00:02.0000000Z` | `until 参数错误：--until 格式必须为 YYYY-MM-DDTHH:MM:SS（秒后可带一至六位小数），并以 Z 或 +00:00 结尾，实际值 '…'` |
| 裸 `--until`（缺值） | `--until`（置于末尾） | `until 参数错误：--until 必须提供值，格式为 YYYY-MM-DDTHH:MM:SS（秒后可带一至六位小数）并以 Z 或 +00:00 结尾，实际缺少值` |

**起点晚于终点**是双端点组合的特有错误（两端点各自合法后才比较时刻，
`healthcheck.py:573-582`）。实测
`--since 2026-10-04T00:00:03Z --until 2026-10-04T00:00:01Z` 的 stderr 为：

```text
healthcheck: error: 时间范围参数错误：--since 给出的起始时刻 '2026-10-04T00:00:03Z' 晚于 --until 给出的结束时刻 '2026-10-04T00:00:01Z'，起点不得晚于终点（二者相等时为只含同一时刻的合法窗口）
```

退出码 `2`、stdout 空、无回溯（`test_since_later_than_until_rejected`，
该用例同时快照确认库与目录不变）。两端点**时刻相等合法**（即使一个写 `Z`、
一个写 `+00:00`），见第 6.4 节。

这些拒绝都发生在数据库访问之前：`--since` 调用点 `healthcheck.py:568`、
`--until` 调用点 `:572`、窗口次序检查 `:573-582`，均先于缺库分支
`:589`。因此：

- **非法时间参数与目录/缺库路径同时出现时只报时间参数**：`--until` 的用例
  `test_invalid_until_with_directory_db_reports_until_first` 把数据库路径
  指向一个目录并给缺时区的 `--until`，预期退出码 2、stderr 含「缺少时区」
  且不含「目录」，目录本身不被改动（`--since` 侧对应
  `test_invalid_since_with_directory_db_reports_since_first`）。
- **`--since` 错误先于 `--until`**：两个时间参数同时非法时
  （`--since bad --until worse`）只报 `since` 错误
  （`test_invalid_since_reported_before_invalid_until`），固定顺序为
  status → URL → reason → since → until（见第 2 节步骤 5）。

### 9.2 记录的 `checked_at` 非法：在窗口之外、limit 之外仍拒绝；被其他条件排除则无视

`--since` / `--until` **任一时间边界生效**时，时间格式校验就针对**全部符合
url/status/reason 的候选记录**，先于窗口比较与 limit 截取
（`filter_rows_by_since`，`healthcheck.py:322-336`）。

- **坏记录即使排在 limit 之外仍使查询失败**：`--since` 侧用例
  `test_bad_checked_at_beyond_limit_still_rejected` 构造三条均为
  A/failure/timeout 的记录——id 1 的 `checked_at` 为 `"not-a-timestamp"`，
  id 2、3 为合法时刻（`00:00:05Z`、`00:00:06Z`）——执行
  `--since 2026-10-04T00:00:00Z --limit 2`。按 id 倒序坏记录 id 1 本会落在
  limit 2 之外（前两条是 id 3、2），但遍历时仍会解析到它：预期退出码 `2`、
  stdout 空、stderr 含 **`id=1`** 与「checked_at 不符合 UTC 时间格式」并回显
  `'not-a-timestamp'`，数据不变。`--until` 侧对应用例
  `test_bad_checked_at_beyond_limit_still_rejected`（`test_recent_until.py`）
  用 `--until 2026-10-04T00:00:10Z --limit 1` 复现同一结论。
  实测为：`healthcheck: error: 记录 id=1 的 checked_at 不符合 UTC 时间格式
  （YYYY-MM-DDTHH:MM:SS，秒后可带一至六位小数，以 Z 或 +00:00 结尾）:
  'not-a-timestamp'`。
- **坏记录即使落在窗口之外仍被拒绝**：校验先于两条边界比较，因此坏记录能否
  被窗口保留与是否报错无关。`--until` 侧
  `test_bad_checked_at_before_since_still_rejected` 中坏记录（id 1，
  `"not-a-timestamp"`）即便按其相邻 id 的时刻推断也早于 `--since
  2026-10-04T00:00:04Z`、本会落在窗口之外，仍以退出码 2、stderr 含 `id=1`
  拒绝；`test_bad_checked_at_after_until_still_rejected` 在只给
  `--until 2026-10-04T00:00:06Z` 时复现「坏记录本会晚于上界被排除、仍报错」。
- **被其他条件排除的坏记录不影响查询**
  （`test_recent_since.py` 与 `test_recent_until.py` 各有一个同名用例
  `test_bad_checked_at_excluded_by_other_filter_ignored`）：坏记录属于 B，
  而查询给 `--url A`，它在 SQL 阶段即被 `WHERE url = ?` 排除、不在 Python
  遍历的 `rows` 中；A 的合法记录正常返回（id 2），退出码 0、stderr 为空。
  同理，被 status/reason 排除的坏记录也不会被校验。
- **两个时间边界都省略时不做任何时间校验**
  （`test_without_since_bad_checked_at_not_validated`）：不带 `--since` 与
  `--until` 走 SQL `LIMIT ?` 分支，`filter_rows_by_since` 根本不被调用，坏
  `checked_at` 记录照常以原始字符串返回（输出里仍能看到
  `"checked_at":"not-a-timestamp"`），不报错，同时也不做任何时刻过滤
  （`test_without_since_ignores_time_and_does_not_validate`：同样的
  A/failure/timeout/limit 2，只给 `--since` 起点时 00:00:01 的 id 4 入选，
  按 id 倒序得 id 4、2；对照第 7.1 节加起点后为 id 2、1）。

### 9.3 无匹配：退出码 0，不是错误

时间窗口过滤后无记录保留（单用 `--until` 早于全部记录、闭合窗口内无记录，
或与其他条件交集为空）时，与其他「合法但无命中」情形完全一致：

- 普通查询输出 `[]`（实测 `--until 2026-10-04T00:00:00Z` 在第 6.3 节
  样本上输出 `[]` 加末尾换行，对应用例 `test_until_no_match_empty` 的普通
  与摘要两种形态）；
- `--summary` 输出
  `{"count":0,"min_elapsed_ms":null,"max_elapsed_ms":null,"avg_elapsed_ms":null}`；
- **数据库不存在时结论相同**：`--db` 指向不存在的路径（含父目录也不存在）
  不报错也不创建任何东西，普通查询 `[]`、摘要 count 0 三 null
  （`test_missing_db_returns_empty_and_creates_nothing`，该用例额外断言
  文件与父目录均未出现）；
- 退出码 `0`、stderr 为空（另见第 7.3 节与第 10.1 节）。

注意区分：**参数/数据格式非法 → 退出码 2**（第 9.1、9.2 节）；
**合法但无记录（或缺库）→ 退出码 0 的空结果**（本节）。

---

## 10. 空结果与错误的区别

两种情况退出码、输出通道完全不同。

### 10.1 空结果：退出码 0，正常输出

| 情形 | 普通查询 stdout | 摘要 stdout | 代码位置 |
|---|---|---|---|
| 数据库文件不存在（父目录也不存在；给不给 `--since`/`--until` 相同） | `[]` | `{"count":0,"min_elapsed_ms":null,"max_elapsed_ms":null,"avg_elapsed_ms":null}` | `healthcheck.py:589-594` |
| 有效数据库但没有历史表（含仅有其他表） | `[]` | 同上（count 0、三个 null） | `623-625` + `672-681` |
| 历史表存在但为空表 | `[]` | 同上 | 查询返回 0 行，`659-682` |
| 合法条件但无匹配记录（URL/status/reason 任一组合无命中） | `[]` | 同上 | 对应 SELECT 返回 0 行 |
| 时间窗口内无保留（`--since` 推进一微秒，第 7.3 节；`--until` 早于全部记录，第 9.3 节） | `[]` | 同上 | `filter_rows_by_since` 返回空，`309-336` |

这些情形 stderr 均为空，且不创建文件/目录、不补建历史表，其他表与数据
保持不变。历史表名按大小写不敏感识别：`CHECKS`、`Checks` 等与 `checks`
得到完全相同的查询结果（回归测试 `test_recent_case_table.py`、
`test_recent_reason_filter.py::test_reason_filter_works_with_case_variant_table`）。

### 10.2 错误：退出码 2，stdout 为空，原因写入 stderr

| 情形 | 报告位置 / 信息要点 |
|---|---|
| 非法 `--status`（非 `success`/`failure`、大小写不符、空字符串、带空白） | `validate_status_filter` → `die`，前缀 `healthcheck: error:`，信息含「status 参数错误」并回显实际值（`healthcheck.py:162-181, 552`） |
| 裸 `--status` 缺少值 | 哨兵 `STATUS_FILTER_MISSING` 被同一校验拒绝，信息说明「--status 必须提供值」（`healthcheck.py:171-175, 727-729`） |
| 非法 `--reason`（非四个固定值、大小写不符、空字符串、前后空白） | `validate_reason_filter` → `die`，前缀 `healthcheck: error:`，信息含「reason 参数错误」并回显实际值（`healthcheck.py:184-207, 563`） |
| 裸 `--reason` 缺少值 | 哨兵 `REASON_FILTER_MISSING` 被同一校验拒绝，信息说明「--reason 必须提供值……实际缺少值」（`healthcheck.py:195-200, 737-739`） |
| 非法 `--since`（缺值、空值、前后空白、缺时区、非 UTC 偏移、非法日期、七位小数等） | `validate_since_filter` / `utc_timestamp_format_problem` → `die`，信息以「since 参数错误：--since …」指出 since 与原因并回显实际值（`healthcheck.py:234-282, 568`）；逐条见第 9.1 节 |
| 非法 `--until`（同左各类取值问题） | `validate_until_filter` 复用同一格式校验 → `die`，信息以「until 参数错误：--until …」指出 until 与原因并回显实际值（`healthcheck.py:285-306, 572`）；逐条见第 9.1 节 |
| `--since` 起点晚于 `--until` 终点（各自合法、相等合法） | 窗口次序检查 → `die`，信息为「时间范围参数错误：……晚于……（二者相等时为只含同一时刻的合法窗口）」（`healthcheck.py:573-582`） |
| 任一时间边界生效时候选记录 `checked_at` 非法（即使落在窗口或 limit 之外） | `filter_rows_by_since` → `die`，信息为「记录 id=<id> 的 checked_at 不符合 UTC 时间格式……」并回显原始值（`healthcheck.py:324-330`）；见第 9.2 节 |
| 非法 `--url`（非 http、非 127.0.0.1、缺端口、端口越界、userinfo、含 `#`、空白或结构非法等） | `validate_target_url` → `die`，前缀 `healthcheck: error:`，并回显非法输入（`healthcheck.py:339-389, 557-558`） |
| 非法 `--limit`（0、负数、`1.5`、非数字） | argparse 在进入 `command_recent` **之前**拒绝，退出码 2、stdout 空、用法与原因写 stderr（`positive_limit`，`healthcheck.py:151-159`） |
| `--db` 路径是目录 | `healthcheck.py:597-598`，信息含「目录」 |
| 历史表（含 `CHECKS`/`Checks` 等异写）缺少任一所需字段 | 查询前由 `ensure_checks_columns` 拒绝，信息为「checks 表缺少字段: …」并列出缺失字段（`healthcheck.py:629, 411-423`）；不再返回空历史 |
| 文件存在但不是有效 SQLite 数据库 | 查询期错误，信息为 `file is not a database`（`healthcheck.py:654-657`） |
| 读取权限不足 | 只读打开/查询失败，信息为 `unable to open database file`（`healthcheck.py:602-606, 654-657`） |

优先级：argparse 先解析 `--limit`，故 limit 非法时根本不会进入处理函数；
进入 `command_recent` 后**先校验 status、再校验 URL、再校验 reason、再校验
since、再校验 until 与窗口次序，最后才判断数据库路径**，因此报告顺序为：
**status 错误 ＞ URL 错误 ＞ reason 错误 ＞ since 错误 ＞ until 错误（含起点
晚于终点）＞ 数据库路径错误**（例如非法 status 配合非法 since/until 与目录
路径，只报 status 错误；status、URL、reason 都合法后，非法 since 先于非法
until 与目录/缺库错误报告，且此时数据库尚未被访问——见第 9.1 节）。所有
`die` 错误都只有 stderr、无 Python 回溯（`die`，`healthcheck.py:135-138`）。

---

## 11. 只读、无副作用保证

- **不发送网络请求**：`recent` 路径不调用 `probe_once`（`healthcheck.py:443-467`），
  URL 仅做本地合法性校验与字符串匹配，`--since`/`--until` 只做本地时间解析
  与比较。回归用例 `test_summary_makes_no_network_requests` 与
  `test_recent_reason_filter.py::test_reason_query_makes_no_network_requests`
  在本地起服务器核对请求数为 0；`test_recent_since.py`、`test_recent_until.py`
  不依赖任何演示服务。
- **不创建文件或目录**：数据库/父目录缺失时直接输出空结果
  （`healthcheck.py:589-594`），不调用会建库的 `open_database`；
  给了合法 `--since`/`--until` 时同样如此。`test_recent_since.py` 与
  `test_recent_until.py` 的每个用例都用 `snapshot_state` 比对查询前后目录
  文件集合与全表内容，确认无新增、无改动（非法时间参数、坏 `checked_at`
  拒绝时也核对了目录/数据不变；缺库用例另外断言文件与父目录均未出现）。
- **不创建表、不改动记录**：库以 `?mode=ro` 打开（`healthcheck.py:602`），
  全程只有 `SELECT`（含对 `sqlite_master` 的查询）；无历史表时仅置空结果。
  任一时间边界生效时也只是把已取到的行在 Python 内过滤，不执行任何写操作。
- **可读但不可写的数据库仍可查询**：只读 URI 不需要写权限，也不生成
  `-wal`/`-journal` 旁路文件（`test_readonly_database_is_queryable` 覆盖了
  带 `--status`、`--reason` 组合条件的只读查询）。

## 12. 保持不变的既有语义

- 默认 `--limit 5`（`DEFAULT_LIMIT`）；未给 `--url`、`--status`、`--reason`、
  `--since`、`--until` 时跨全部目标按 id 倒序返回（`SELECT_SQL`）。
- **省略 `--status`、`--reason`、`--since`、`--until` 的查询输出与新增这些
  参数前完全一致**：普通查询仍返回成功与失败混合、各原因混合、不按时间过滤
  的记录数组，摘要仍对同一批记录统计（样本见
  `test_omitting_status_keeps_legacy_behavior`、
  `test_omitting_reason_keeps_legacy_behavior`、
  `test_recent_since.py::test_without_since_ignores_time_and_does_not_validate`
  与 `test_recent_until.py::test_without_until_results_unchanged`——后者证明
  仅新增 `--until` 不影响只给 `--since` 的查询结果）。
- **产品代码的 `check` 行为、表结构与退出码约定均未因本文说明而改变**：
  `check` 子命令不接受 `--status`、`--reason`、`--since`，也不接受
  `--until`（`test_check_does_not_accept_until`：argparse 报
  `unrecognized arguments`）；其探测与写入行为不变：`probe_once` 只发一次
  GET、不跟随重定向，成功/失败经 `INSERT_SQL` 落库后才输出
  （`command_check`，`healthcheck.py:474-516`）；建库建表仍只由 `check` 的
  `open_database` 完成。reason 的取值仍只由探测结果决定
  （2xx→ok、非 2xx→http_status、超时→timeout、其他连接错误→
  connection_error），recent 的 `--reason` 只读这些已保存的值。记录的
  `checked_at` 由 `utc_now_iso`（`healthcheck.py:470-471`）以 ISO 形式写入，
  recent 的 `--since`/`--until` 只读已保存值并按时刻比较。
- 记录七字段、表结构（`CREATE_TABLE_SQL`，`healthcheck.py:82-93`）与退出码
  约定（成功 0 / 已记录的探测失败 1 / 参数或数据库错误 2）均不变。

## 13. 结论到源码位置的对照

| 结论 | 函数 / 常量 | 位置 |
|---|---|---|
| recent 处理总流程 | `command_recent` | `546-697` |
| status 必须为区分大小写的 success/failure，且最先校验 | `validate_status_filter` / 哨兵 `STATUS_FILTER_MISSING` | `162-181` / `43`，调用点 `552` |
| URL 合法性规则（先于 reason/since/until/DB 路径、晚于 status 执行） | `validate_target_url` | `339-389`，调用点 `557-558` |
| reason 必须为区分大小写的四个固定值；status、URL 之后、时间参数/DB 之前校验 | `validate_reason_filter` / 哨兵 `REASON_FILTER_MISSING` / `REASON_FILTER_CHOICES` | `184-207` / `50` / `54-59`，调用点 `563` |
| since 合法格式与真实性；status/URL/reason 之后、until/DB 之前校验 | `validate_since_filter` / `utc_timestamp_format_problem` / 哨兵 `SINCE_FILTER_MISSING` | `262-282` / `234-259` / `63`，调用点 `568` |
| until 与 since 同规则（`<=` 上界、单用不设下界）；在 since 之后、DB 之前校验 | `validate_until_filter` / 哨兵 `UNTIL_FILTER_MISSING` | `285-306` / `67`，调用点 `572` |
| 双端点窗口：两端均包含、相等合法；起点晚于终点以退出码 2 拒绝 | `command_recent` 窗口次序检查 | `573-582` |
| since/until/checked_at 的严格 UTC 正则与等价解析（Z≡+00:00、零小数≡省略） | `UTC_TIMESTAMP_RE` / `UTC_TIMESTAMP_BODY_RE` / `parse_utc_timestamp` | `72-80` / `78-80` / `210-231` |
| 任一时间边界生效：逐条校验 checked_at、按 `>=since`/`<=until` 保留、先于 limit 限量 | `filter_rows_by_since` | `309-336`，调用点 `649-651` |
| limit 必须为正整数、默认 5 | `positive_limit` / `DEFAULT_LIMIT` | `151-159` / `31` |
| 缺库（含父目录缺失）视为空，不创建 | 路径存在性分支 | `589-594` |
| 目录路径报错（退出码 2） | 目录分支 + `die` | `597-598` |
| 只读打开、不可写也能查 | `?mode=ro` URI | `602-606` |
| 历史表名大小写不敏感识别（CHECKS/Checks 同 checks） | `sqlite_master` 判断 | `611-622` |
| 无历史表视为空、不建表 | 表缺失分支 | `623-625` |
| 历史表缺字段以退出码 2 报缺列（不误判为空历史） | `ensure_checks_columns` | `411-423`，调用点 `629` |
| 唯一查询模板：按 id 倒序；WHERE 与 LIMIT 由组合/是否有时间边界生成 | `SELECT_SQL` / `RECENT_FILTER_CLAUSES` | `112-118` / `122-126` |
| 库内三筛选统一组装：条件 AND，先筛选；`sql_limit` 控制是否 SQL 限量 | `build_recent_query` | `519-543`，调用点 `636-648` |
| URL 条件：原始字符串精确匹配 | `url = ?` 条件片段 | `122-126` |
| status 条件：按记录 status 等值匹配（不看 reason） | `status = ?` 条件片段 | `122-126` |
| reason 条件：按保存的 reason 精确匹配（不看 status/http_status） | `reason = ?` 条件片段 | `122-126` |
| 摘要统计同条件最终记录子集；失败与零耗时计入；均值不取整 | 摘要分支 | `659-682` |
| 空摘要固定输出（count 0、三个 null） | `render_recent_result` / `build_elapsed_summary`（空记录集） | 呈现入口与摘要构造 |
| 完整七字段记录数组 / `[]` 输出（checked_at 原样保留） | 记录映射与 JSON 输出 | `684-696` |
| 错误统一出口（退出码 2、stderr、stdout 空） | `die` | `135-138` |
| 网络探测与建库/写入仅属于 check（recent 不触碰；check 不接受 --until） | `probe_once` / `open_database` / `INSERT_SQL` | `443-467` / `392-408` / `95-98` |
