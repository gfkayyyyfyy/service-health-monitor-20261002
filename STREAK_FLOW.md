# `streak` 连续失败与阈值查询流程说明

本说明描述 `healthcheck.py` **当前已实现**的 `streak` 子命令从命令行输入到
单行 JSON 输出的完整行为，不新增、不修改任何行为：`check` / `recent` /
`streak` 的公开接口、数据库结构与既有文档保持不变，本次交付只新增本文档。
内容沿「参数解析 → 目标 URL 校验 → 阈值校验 → 数据库只读打开 → 历史表解析
→ 单目标 `(id, status)` 倒序查询 → Python 侧连续失败段计数 →（可选）阈值
判断 → 单行 JSON 输出」讲清每一步，关键结论均标注对应的源码函数或位置
（行号以当前 `healthcheck.py` 为准）。

- 入口函数：`main`（`healthcheck.py:1045-1048`）→ `command_streak`
  （`healthcheck.py:864-921`）
- 连续段计数：`count_consecutive_failures`（`healthcheck.py:822-842`）
- 唯一输出入口：`render_streak_result`（`healthcheck.py:845-861`）
- 唯一查询模板：`STREAK_SQL`（`healthcheck.py:141-146`）
- `streak` **不发送网络请求、不创建或改写文件/目录/表/记录、不生成任何告警
  事件**；数据库以只读 URI 打开，可读但不可写的库仍可查询（见第 9 节）。

> 文中所有命令输出块（stdout / 退出码 / stderr）均为**源码推导的预期结果**：
> 依据当前实现逐条核对（对应验收测试 `test_streak.py`），表示在第 6 节样本库
> 上运行时应当得到的结果。撰写本文时，这些预期已照第 7 节命令在该样本上实际
> 运行复核、与推导一致；输出块本身仍是「应当得到什么」的规格，而非某次运行的
> 转存。你可照第 6 节准备样本库并按第 7 节自行复现。

---

## 1. 命令形态与必填参数

```
python healthcheck.py --db <数据库路径> streak --url <目标 URL> [--threshold N]
```

参数在 `build_parser` 的 streak 子解析器中定义（`healthcheck.py:1015-1040`）：

| 参数 | 必填 | 定义位置 | 说明 |
|---|---|---|---|
| `--db` | 是（全局） | `healthcheck.py:929`（`required=True`） | SQLite 数据库文件路径 |
| `streak` | 是（子命令） | `healthcheck.py:930`、`1015-1018` | 选择连续失败查询 |
| `--url` | 是 | `healthcheck.py:1019-1026`（`required=True`） | 目标 URL，按库中保存的 url 原文精确匹配 |
| `--threshold` | 否 | `healthcheck.py:1027-1039`（`nargs="?"`，默认 `None`） | 连续失败次数阈值，规则见第 4 节 |

- 缺少 `--db` 或 `--url`：由 argparse 在进入 `command_streak` 之前拒绝，
  **退出码 2、stdout 为空**，argparse 的用法与错误信息写入 stderr。
- streak 子解析器**只定义了 `--url` 与 `--threshold`**：`recent` 的
  `--limit` / `--status` / `--reason` / `--since` / `--until` / `--summary`
  / `--status-summary` 以及任何未知选项，在 streak 下都被 argparse 以退出码
  2 拒绝（对应测试 `test_unsupported_recent_options_rejected`、
  `test_unknown_subcommand_or_option`）。

解析得到的参数交给 `command_streak(args)`（由 `main` 在
`healthcheck.py:1048` 通过 `args.handler(args)` 分发）。

## 2. 本机 URL 校验规则（先于一切数据库访问）

`command_streak` 的**第一步**是 `validate_target_url(args.url)`
（`healthcheck.py:867`；规则函数 `healthcheck.py:394-444`）。校验只判定输入
合法性，**其返回值 `(port, target)` 在 streak 路径被丢弃**——匹配时仍用
命令行原始字符串 `args.url`，不做任何规范化（见第 3 节）。

只接受形态 `http://127.0.0.1:<显式端口>[/path][?query]`，逐条规则：

1. 必须是非空字符串；空值报错（`healthcheck.py:396-397`）。
2. 不得含任何空白或 ASCII 控制字符（`healthcheck.py:399-400`）。
3. 必须能被 `urlparse` 解析；方括号不成对等结构非法被拒绝
   （`healthcheck.py:402-407`）。
4. 协议必须恰为 `http`：`https`、`ftp` 等一律拒绝
   （`healthcheck.py:416-417`）。
5. 主机必须恰为 `127.0.0.1`（常量 `ALLOWED_HOST`，`healthcheck.py:32`；
   判定 `healthcheck.py:418-419`）：`localhost`、`0.0.0.0`、`127.0.0.2`、
   本机名等均拒绝。
6. 必须**显式**给出端口，且端口在 1-65535 之间（`healthcheck.py:420-428`）：
   `http://127.0.0.1/health` 因缺端口被拒绝。
7. 不接受 userinfo（用户名/密码）（`healthcheck.py:429-430`）。
8. 原始字符串中不得出现 `#`，即不接受片段（fragment），空片段也拒绝
   （`healthcheck.py:431-436`）；`%23` 是普通百分号编码内容，不含原始
   `#`，仍合法。

非法 URL 一律经统一错误出口 `die`（`healthcheck.py:148-151`）：向 stderr 写
一行 `healthcheck: error: …`，**退出码 2、stdout 保持为空、无 Python 回溯**。
URL 校验发生在阈值校验与数据库打开之前（见第 5 节顺序），拒绝时不读库。

## 3. 目标精确匹配与连续失败段算法

### 3.1 严格只读打开与历史表解析

URL、阈值都合法后才打开数据库（`healthcheck.py:879`
`open_history_db_readonly`，`healthcheck.py:486-504`）：

- 路径（含父目录）不存在 → 返回 `None`，表示历史为空：**不创建文件、不创建
  目录**，也不产生 `-wal`/`-journal`（`healthcheck.py:494-495`，快路径
  `healthcheck.py:880-882`）。
- 路径指向一个**目录** → 这是错误，退出码 2（`healthcheck.py:496-497`）。
- 以只读 URI（`?mode=ro`）打开（`healthcheck.py:500-502`）：可读但不可写的
  文件也能查询；打开/读取失败（无效 SQLite、无读权限等）经 `die` 退出码 2
  （`healthcheck.py:503-504`，查询期异常在 `healthcheck.py:913-916` 同样
  处理）。

历史表名按**大小写不敏感**解析：`resolve_checks_table`
（`healthcheck.py:507-519`）查 `sqlite_master`，把 `CHECKS` / `Checks` /
`checks` 视为同一张历史表（这类异写表至多一张），返回库中保存的实际表名；
空库或仅有其他表时返回 `None`（`healthcheck.py:888-892` 视为空结果）。

表存在时先核对必需字段：`ensure_checks_columns`（`healthcheck.py:466-478`）
按 `REQUIRED_COLUMNS`（`healthcheck.py:110-112`，七列 `id, url, checked_at,
elapsed_ms, status, http_status, reason`）逐一检查，**缺任意一列即以退出码
2 报缺列**（列名比较大小写不敏感；额外列、列顺序不同不构成问题）。注意
streak 的 SELECT 虽只读取 `id`、`status` 两列，仍沿用与 recent/check 相同的
七列完整性门槛，不单独放宽。

### 3.2 查询：单目标、按 id 倒序、不加 LIMIT

通过校验后，表名经 `quote_identifier` 加双引号引用（`healthcheck.py:481-483`、
`896`），执行唯一查询模板 `STREAK_SQL`（`healthcheck.py:141-146`，执行点
`healthcheck.py:899-901`）：

```sql
SELECT id, status
FROM <历史表>
WHERE url = ?
ORDER BY id DESC
```

- **目标按保存的 URL 原文精确匹配**：绑定参数就是 `args.url`（绑定点
  `healthcheck.py:900`），以 `url = ?` 做字节级等值比较，不做任何规范化。
  因此 `http://127.0.0.1:8765/health` 与
  `http://127.0.0.1:8765/health?detail=1`、与结尾多一个 `/` 的写法都是不同
  目标；非 ASCII（如中文路径）也按保存原文匹配并在输出中原样回显
  （`ensure_ascii=False`，`healthcheck.py:861`；对应测试
  `test_url_exact_verbatim_match_no_normalization`、
  `test_unicode_url_echoed_verbatim`）。
- **连续段只按 id 大小定义先后**：`ORDER BY id DESC` 是唯一排序，
  **`checked_at` 不参与排序或判断**——即使 id 较大的记录时间更早，仍以 id
  倒序为准（对应测试 `test_ordering_by_id_not_checked_at`）。
- **不带 LIMIT**：连续失败次数不受 recent 默认五条（`DEFAULT_LIMIT`，
  `healthcheck.py:34`）的限制，目标有多少条就读多少条（对应测试
  `test_not_capped_by_recent_default_limit_five`，12 连败计为 12）。
- **其他目标不参与**：`WHERE url = ?` 在 SQL 层就把别的 url 记录排除，其他
  目标的成功或失败都不会出现在结果行里（第 7 节专门解释 id=3）。

### 3.3 计数：自最大 id 起累计 failure，遇首条 success 即止

结果行交给 `count_consecutive_failures`（`healthcheck.py:822-842`，调用点
`healthcheck.py:910`）。行已按 id 从大到小排列且同属一个目标，算法为
（`healthcheck.py:831-842`）：

- 自最大 id 起逐条看保存的 `status`；
- `failure`（常量 `STATUS_FAILURE`，`healthcheck.py:42`）→ 计数加一；
- 第一条 `success`（`STATUS_SUCCESS`，`healthcheck.py:41`）→ **立即停止**，
  比它更旧的记录不再遍历；
- 最新一条就是 `success` 时计数为 0（但仍保留 `latest_id`）；全部为
  `failure` 时统计全部；
- 出现 `success`/`failure` 之外的取值 → 经 `die` 退出码 2，并**指出记录
  id**（`healthcheck.py:837-841`），不修改任何数据（见第 8.3 节）。

判断**只依据保存的 `status` 字段**：`reason` 与 `http_status` 完全不参与。
例如保存为 `failure` 但 `http_status=200`、`reason="ok"` 的记录仍计为失败
（对应测试 `test_only_saved_status_matters_not_reason_or_http_status`）。

`latest_id` 取结果行第一行的 id，即该目标匹配记录中的**最大 id**
（`healthcheck.py:906` `latest_id = rows[0][0]`），与该记录的成功/失败无关；
目标无匹配记录时为 `null`（`healthcheck.py:902-904`）。

## 4. `--threshold` 取值规则与输出字段

### 4.1 取值校验：纯 ASCII 数字、大于零、允许前导零

阈值由 `validate_threshold` 校验（`healthcheck.py:316-361`，调用点
`healthcheck.py:873`）。三种形态：

- **省略** `--threshold`：argparse 默认 `None`（`healthcheck.py:1031`），
  `validate_threshold` 直接返回 `None`（`healthcheck.py:325-326`）→ 不做阈值
  判断，保留原有三字段输出。
- **裸 `--threshold`（命令行上未给值）**：argparse 注入哨兵
  `THRESHOLD_MISSING`（`healthcheck.py:69-71`、`1029-1030`），经 `die` 报
  「必须提供值」（`healthcheck.py:327-331`）。
- **给出值**：只接受能被 `THRESHOLD_DIGITS_RE = [0-9]+\Z` 整体匹配的纯 ASCII
  十进制数字文本（`healthcheck.py:73-75`、`333`），且 `int(text)` 后数值
  **大于零**（`healthcheck.py:356-360`）。前导零合法且不保留，输出按整数
  表示（如 `002` → `2`）；任意长度数字串均可，streak 路径会解除 Python
  3.11+ 的整数字符串位数限制（`disable_int_digit_limit_for_threshold`，
  `healthcheck.py:301-313`、`347`）。

下列取值全部为错误，经 `die` **退出码 2、stdout 为空**，stderr 指出
`threshold` 与具体原因（对应测试
`test_threshold_invalid_values_rejected_before_db_access`）：

| 非法类别 | 示例 | 拒绝位置 / 原因 |
|---|---|---|
| 缺值（裸参数） | `--threshold` | `healthcheck.py:327-331` |
| 空值 | `--threshold ""` | `healthcheck.py:334-335`「值不能为空」 |
| 零 | `0`、`000` | 数字合法但数值非正，`healthcheck.py:356-360` |
| 负数 | `-1`、`-0` | 含非数字字符，`healthcheck.py:339-346` |
| 小数 | `1.5`、`.5` | 含小数点 |
| 正号 | `+3` | 含符号 |
| 空白 | ` 3`、`3 `、`\t3`、`3\n` | `healthcheck.py:336-337`「前后不允许有空白」 |
| 内嵌空白/其他数字写法 | `1 2`、`1e3`、`0x1` | 非纯数字字符 |
| 非 ASCII 数字 / 文字 | `９`（全角）、`three` | 非 ASCII 数字字符 |

### 4.2 输出：三字段或五字段，单行紧凑 JSON

唯一输出入口 `render_streak_result`（`healthcheck.py:845-861`，调用点
`healthcheck.py:918-920`）以 `json.dumps(..., separators=(",", ":"),
ensure_ascii=False)` 打印**一行紧凑 JSON 加一个换行**：

- **省略阈值**：恰含三字段，顺序固定 `url`、`latest_id`、
  `consecutive_failures`（`healthcheck.py:853-857`）。
- **提供阈值**：在其后追加整数 `threshold` 与布尔 `threshold_reached`
  （`healthcheck.py:858-860`）；判断式为
  `consecutive_failures >= threshold`，即**次数大于或等于阈值为 `true`**
  （边界取等，对应测试 `test_threshold_boundary_is_greater_or_equal`）。
- `url` 保留命令行输入原文；`threshold` 恒为 JSON 整数（不出现 `"002"` 这样
  的字符串），`threshold_reached` 恒为布尔（对应测试
  `test_threshold_leading_zeros_output_as_integer`）。

正常查询（含各种空结果）**退出码均为 0、stderr 为空**（`command_streak`
返回 0，`healthcheck.py:882`、`921`）。

## 5. 校验与访问顺序

`command_streak` 体内顺序固定（`healthcheck.py:864-921`）：

1. **URL 校验**（`healthcheck.py:867`）；
2. **阈值校验**（`healthcheck.py:873`）；
3. **才打开数据库**（`healthcheck.py:879`）。

即 **URL 校验先于阈值校验，两者先于数据库访问**。实测可复核：同时给非法
URL、非法阈值并指向不存在的库时，只报 URL 错误；URL 合法、阈值非法时，即使
库路径不存在也只报阈值错误，且不创建任何目录（对应测试
`test_invalid_url_rejected_before_db_access`、
`test_threshold_invalid_values_rejected_before_db_access`）。argparse 层级的
错误（缺 `--db`/`--url`、未知选项）更早，由 argparse 直接以退出码 2 拒绝。

## 6. 固定样本（复用 `test_streak.py` 的四行样本）

样本来自 `test_streak.py` 的 `test_spec_example_interleaved_other_target` 与
`test_threshold_spec_example`：两个目标，目标 A 占 id 1、2、4，**另一目标 B
占 id 3**。由测试夹具 `R()`（`test_streak.py:71-75`）与 `build_db()`
（`test_streak.py:57-68`）生成，`elapsed_ms` 固定为 0，`checked_at` 取
`R()` 的默认值 `2026-10-04T00:00:{id:02d}.000000+00:00`。四行的**完整七
字段**如下：

| id | url | checked_at | elapsed_ms | status | http_status | reason |
|----|-----|-----------|-----------|--------|-------------|--------|
| 1 | `http://127.0.0.1:8765/health` | `2026-10-04T00:00:01.000000+00:00` | 0 | `success` | 200 | `ok` |
| 2 | `http://127.0.0.1:8765/health` | `2026-10-04T00:00:02.000000+00:00` | 0 | `failure` | null | `connection_error` |
| 3 | `http://127.0.0.1:8765/ready` | `2026-10-04T00:00:03.000000+00:00` | 0 | `success` | 200 | `ok` |
| 4 | `http://127.0.0.1:8765/health` | `2026-10-04T00:00:04.000000+00:00` | 0 | `failure` | 500 | `http_status` |

其中 id 2 的 `http_status` 为 NULL（连接类失败没有状态码），id 4 为 500；
两者 `reason` 不同，但连续段只看 `status`，这些差异不影响计数。

### 6.1 可复现的本地样本准备

只用 Python 3 标准库直接建库，**不发任何网络请求**。下表结构与
`test_streak.py` 的 `SCHEMA`（`test_streak.py:37-47`）一致——测试刻意把
`status` 声明为无 CHECK 的 `TEXT`，以便同一夹具还能插入非法 status 用例；
本样本四行在产品建表语句 `CREATE_TABLE_SQL`（`healthcheck.py:90-101`，带
`status`/`reason` 的 CHECK 约束）下同样全部合法。

```sh
python3 - <<'EOF'
import sqlite3

URL_A = "http://127.0.0.1:8765/health"
URL_B = "http://127.0.0.1:8765/ready"

conn = sqlite3.connect("monitor.sqlite")
conn.execute("""
CREATE TABLE checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    elapsed_ms INTEGER NOT NULL CHECK (elapsed_ms >= 0),
    status TEXT,
    http_status INTEGER,
    reason TEXT NOT NULL
)
""")
conn.executemany(
    "INSERT INTO checks (id, url, checked_at, elapsed_ms, status, "
    "http_status, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
    [
        (1, URL_A, "2026-10-04T00:00:01.000000+00:00", 0,
         "success", 200, "ok"),
        (2, URL_A, "2026-10-04T00:00:02.000000+00:00", 0,
         "failure", None, "connection_error"),
        (3, URL_B, "2026-10-04T00:00:03.000000+00:00", 0,
         "success", 200, "ok"),
        (4, URL_A, "2026-10-04T00:00:04.000000+00:00", 0,
         "failure", 500, "http_status"),
    ],
)
conn.commit()
conn.close()
EOF
```

## 7. 两次本地查询：输入与完整 JSON 预期（源码推导）

以下两条命令在第 6 节样本库上运行；预期均为**源码推导**，并已实际运行核对
一致。两次退出码均为 `0`、stderr 均为空。

### 7.1 省略阈值：三字段，latest_id=4、连续失败 2

输入：

```sh
python healthcheck.py --db monitor.sqlite streak --url http://127.0.0.1:8765/health
```

完整 stdout 预期（一行 JSON + 换行）：

```json
{"url":"http://127.0.0.1:8765/health","latest_id":4,"consecutive_failures":2}
```

推导：`WHERE url = ?` 只取目标 A 的 id 1/2/4，按 id 倒序为 4 → 2 → 1；
id 4 是 `failure`（计 1），id 2 是 `failure`（计 2），id 1 是第一条
`success`，停止。`latest_id` 为最大 id 4。

### 7.2 传入 `002`：五字段，threshold=2、threshold_reached=true

输入：

```sh
python healthcheck.py --db monitor.sqlite streak --url http://127.0.0.1:8765/health --threshold 002
```

完整 stdout 预期（一行 JSON + 换行）：

```json
{"url":"http://127.0.0.1:8765/health","latest_id":4,"consecutive_failures":2,"threshold":2,"threshold_reached":true}
```

推导：`002` 通过纯数字校验，`int("002") = 2`，前导零不保留，故输出整数
`2`；计数仍为 2，`2 >= 2` 为 `true`。同一样本把阈值改为 `3` 时计数不变、
`threshold_reached` 为 `false`（对应测试 `test_threshold_spec_example`）。

### 7.3 为什么 id=3（另一目标的成功）不打断连续段

id=3 保存的是目标 B（`…/ready`）的 `success`，但它**不属于被查询目标**：
`STREAK_SQL` 的 `WHERE url = ?` 绑定的是目标 A 的原文
（`http://127.0.0.1:8765/health`），id=3 这一行在 SQL 阶段就不会被取出。
连续段完全在「目标 A 自己的记录按 id 倒序」这一序列（4、2、1）上计算，其他
目标的成功既不会提前终止计数，也不会把计数清零——终止信号只可能来自**同一
目标**序列里的第一条 `success`（此处是 id=1）。作为对照，对目标 B 单独查询
会得到 `latest_id=3、consecutive_failures=0`（最新即成功），两个目标各自独立
计数（`test_streak.py:159-160`）。

## 8. 空结果与错误的区分

### 8.1 空结果（不是错误）：退出码 0，`latest_id=null`、次数 0

下列情形都走**空结果**分支，退出码 `0`、stderr 为空，省略阈值时输出
`{"url":…,"latest_id":null,"consecutive_failures":0}`；**提供阈值时追加
`"threshold":N,"threshold_reached":false`**（0 次永不大于或等于正阈值）。

| 情形 | 判定位置 |
|---|---|
| 数据库文件不存在（**父目录也不存在同样如此**，不创建任何东西） | `open_history_db_readonly` 返回 `None`，`healthcheck.py:494-495`、快路径 `880-882` |
| 空库（文件存在但无任何表） | `resolve_checks_table` 返回 `None`，`healthcheck.py:888-892` |
| 只有其他表、无历史表 | 同上（`healthcheck.py:507-519`） |
| 历史表存在但为空表 | SELECT 返回 0 行，`healthcheck.py:902-904` |
| 有历史记录但无该 url 的匹配记录 | 同上 |

空结果三例的三字段输出相同，例如（对空库、仅有其他表、url 无匹配分别运行，
已核对）：

```json
{"url":"http://127.0.0.1:8765/health","latest_id":null,"consecutive_failures":0}
```

带阈值（如 `--threshold 5`）时：

```json
{"url":"http://127.0.0.1:8765/health","latest_id":null,"consecutive_failures":0,"threshold":5,"threshold_reached":false}
```

对应测试：`test_no_matching_record`、
`test_missing_db_and_missing_parent_creates_nothing`、
`test_empty_db_and_other_table_only`、`test_history_table_exists_but_empty`、
`test_threshold_empty_results_false`。

### 8.2 数据库侧错误：退出码 2、stdout 为空、原因写 stderr

下列情形**不是空结果而是错误**，经 `die` 退出码 2，stdout 为空，stderr 给
原因（阈值合法与否都不改变这一数据库侧结论，对应测试
`test_threshold_db_errors_still_exit_two_when_threshold_valid`）：

| 情形 | stderr 要点 | 位置 |
|---|---|---|
| `--db` 路径是一个目录 | `读取数据库 … 失败: 路径是一个目录，不是 SQLite 文件` | `healthcheck.py:496-497` |
| 文件不是有效 SQLite 数据库 | `读取数据库 … 失败: file is not a database` | 打开/查询异常，`healthcheck.py:503-504`、`913-916` |
| 无读权限等读取失败 | `读取数据库 … 失败: …` | 同上 |
| 历史表缺任一必需字段 | `checks 表缺少字段: …` | `ensure_checks_columns`，`healthcheck.py:466-478`（调用点 `895`） |

注意缺库（父目录缺失）与「路径是目录」的边界：文件路径与父目录**都不存在**
时按空结果处理且不创建目录；而路径本身**存在且是目录**时是错误。

### 8.3 连续段内的非法 status：退出码 2 并指出记录 id

`count_consecutive_failures` 在遍历中遇到 `success`/`failure` 之外的取值即
`die`，stderr 形如
`记录 id=<id> 的 status 不是受支持的取值（仅 'success' 或 'failure'）: '<值>'`
（`healthcheck.py:837-841`），stdout 为空、退出码 2：

- **即使此前失败次数已达到阈值也照样拒绝**：阈值判断只在完整计数成功后才在
  输出阶段进行，计数中途的非法 status 优先报错，不会输出
  `threshold_reached:true`（对应测试
  `test_threshold_unknown_status_rejected_even_after_reached`，最新两条失败
  已达阈值 2、更早的 id=3 非法，仍退出码 2 且 stderr 含 `id=3`）。
- **比首条 `success` 更旧的记录不影响结果**：遇到第一条 `success` 就停止，
  更早的行（即使 status 非法）根本不会被遍历到（对应测试
  `test_stops_at_first_success_and_ignores_older_rows`、
  `test_threshold_unknown_status_below_first_success_unseen`）。
- **其他目标的非法 status 无关**：被 `WHERE url=?` 排除，不进入序列
  （对应测试 `test_unknown_status_on_other_target_does_not_matter`）。

## 9. 兼容性范围与只读、无副作用保证

- **历史表名大小写兼容**：`CHECKS` / `Checks` / `checks` 识别为同一张历史
  表，查询使用库中保存的实际表名（加双引号引用）；这类异写表至多一张
  （`resolve_checks_table`，`healthcheck.py:507-519`；对应测试
  `test_case_insensitive_table_names`、
  `test_threshold_with_case_table_and_readonly_db`）。
- **可读不可写的数据库可查询**：以 `?mode=ro` 只读 URI 打开，不需要写权限；
  权限位为只读（如 `0400`）的库仍能正常查询，且不产生
  `-wal`/`-journal` 旁路文件（`healthcheck.py:500-502`；对应测试
  `test_readonly_db_still_queries_and_modifies_nothing`）。
- **不创建或改写文件、表和记录**：缺库/缺父目录直接走空结果快路径，不调用
  会建库建表的 `open_database`；只读连接上全程只有 `SELECT`（含对
  `sqlite_master` 与 `PRAGMA table_info` 的读取），无历史表时仅置空结果，
  不补列、不建表、不写任何记录。测试以查询前后目录文件集合 + 全表内容快照
  逐一比对确认（`snapshot`，`test_streak.py:78-92`，多个用例调用）。
- **不发网络请求**：streak 路径不调用 `probe_once`
  （`healthcheck.py:539-563`），URL 仅做本地合法性校验与字符串等值匹配，不
  建立任何 HTTP 连接。
- **不生成告警事件**：程序中没有任何告警/事件/通知代码路径，streak 只是一次
  只读查询加一段 JSON 输出，不会因达到阈值而触发或写入任何告警；
  `threshold_reached` 仅作为输出字段返回，是否据其告警完全由调用方决定。

## 10. 结论到源码位置的对照

| 结论 | 函数 / 常量 | 位置（`healthcheck.py`） |
|---|---|---|
| streak 处理总流程 | `command_streak` | `864-921` |
| 程序入口与子命令分发 | `main` | `1045-1048` |
| `--db` 必填；streak 的 `--url` 必填、`--threshold` 可选 | `build_parser` | `929`；`1015-1040`（url `1019-1026`，threshold `1027-1039`） |
| 本机 URL 规则（http / 127.0.0.1 / 显式端口 / 无 userinfo / 无 `#`） | `validate_target_url` / `ALLOWED_HOST` | `394-444`（调用点 `867`）/ `32` |
| 阈值合法形态：纯 ASCII 数字、大于零、允许前导零 | `validate_threshold` / `THRESHOLD_DIGITS_RE` / `THRESHOLD_MISSING` | `316-361`（调用点 `873`）/ `73-75` / `69-71` |
| 超长数字串解除位数限制（仅 streak 路径） | `disable_int_digit_limit_for_threshold` | `301-313`（调用点 `347`） |
| 顺序：URL → 阈值 → 数据库 | `command_streak` 内三步 | `867` → `873` → `879` |
| 只读打开；缺库（含父目录缺失）为空、目录为错误；可读不可写可查 | `open_history_db_readonly` | `486-504`（快路径 `880-882`，目录 `496-497`，ro URI `500-502`） |
| 历史表名大小写不敏感解析 | `resolve_checks_table` | `507-519`（调用点 `888`） |
| 七列必需字段核对，缺列退出码 2 | `ensure_checks_columns` / `REQUIRED_COLUMNS` | `466-478`（调用点 `895`）/ `110-112` |
| 表名引用 | `quote_identifier` | `481-483`（调用点 `896`） |
| 单目标 `(id,status)` 倒序、无 LIMIT | `STREAK_SQL` | `141-146`（执行点 `899-901`） |
| 连续段：failure 累计、首条 success 止、非法 status 报 id | `count_consecutive_failures` / `STATUS_SUCCESS` / `STATUS_FAILURE` | `822-842`（调用点 `910`）/ `41` / `42` |
| `latest_id` 为该目标最大 id；无记录为 null | 赋值 / 空行分支 | `906` / `902-904` |
| 三字段 / 五字段输出，`>=` 判定，紧凑单行 JSON | `render_streak_result` | `845-861`（三字段 `853-857`，追加字段 `858-860`，调用点 `918-920`） |
| 正常查询退出 0、stderr 空 | `command_streak` 返回值 | `882`、`921` |
| 错误统一出口（退出码 2、stderr 一行、stdout 空、无回溯） | `die` | `148-151` |
| 不发网络请求（streak 不调用） | `probe_once`（仅 check 路径用） | `539-563` |
