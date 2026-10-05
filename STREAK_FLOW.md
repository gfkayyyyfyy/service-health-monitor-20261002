# `streak` 连续失败与阈值查询流程说明

本说明描述 `healthcheck.py` **当前已实现**的 `streak` 子命令从命令行输入到
单行 JSON 输出的完整行为，不新增、不修改任何行为：程序、`check`/`recent`
公开接口、数据库结构与现有文档保持不变。内容沿「参数解析 → URL 校验 →
阈值校验 → 数据库只读查询 → 连续段计算 → 单行 JSON 输出」讲清每一步，
关键结论均标注对应的源码函数或位置（行号以当前 `healthcheck.py` 为准）。

- 入口函数：`main`（`healthcheck.py:1045-1048`）→ `command_streak`
  （`healthcheck.py:864-921`）
- 连续段计算：`count_consecutive_failures`（`healthcheck.py:822-842`）
- 输出构造：`render_streak_result`（`healthcheck.py:845-861`）
- `streak` **不发送网络请求、不创建文件/目录/表、不改动已有记录、不生成
  告警事件**；数据库以只读 URI 打开，可读但不可写的库仍可查询（见第 9 节）。

> 文中所有命令输出块均为**源码推导的预期结果**（依据当前实现逐条核对，核对
> 方式见 `test_streak.py`），表示在第 6 节样本库上运行时应当得到的
> stdout / 退出码 / stderr；它们不是「本文档生成时已实际运行」的实测记录。
> 你可照第 6.2 节准备样本库并按第 7 节命令自行复现。

---

## 1. 命令行与必填参数

参数解析由 `build_parser`（`healthcheck.py:924-1042`）完成，`streak`
子解析器定义于 `healthcheck.py:1015-1040`：

| 参数 | 必填 | 规则 | 代码位置 |
|---|---|---|---|
| `--db` | 必填 | SQLite 数据库文件路径（全局参数，子命令之前给出） | `healthcheck.py:929` |
| `streak` | 必填 | 子命令名 | `healthcheck.py:1015-1018` |
| `--url` | 必填 | 目标 URL，校验规则同 `check`（见第 2 节） | `healthcheck.py:1019-1026` |
| `--threshold` | 可选 | 连续失败次数阈值（见第 3 节）；`nargs="?"`，省略为 `None`，裸参数注入哨兵 `THRESHOLD_MISSING` | `healthcheck.py:1027-1039`，哨兵定义 `healthcheck.py:69-71` |

`streak` **不支持 `recent` 的任何筛选与限量选项**（`--limit`/`--status`/
`--reason`/`--since`/`--until`/`--summary`/`--status-summary`）：这些参数未在
streak 子解析器中注册，argparse 一律以退出码 `2` 拒绝（用法说明写 stderr，
stdout 为空）。缺少 `--db` 或 `--url`、未知选项同样由 argparse 以退出码 `2`
拒绝（对应测试 `test_missing_url`、`test_missing_db`、
`test_unsupported_recent_options_rejected`、`test_unknown_subcommand_or_option`）。

## 2. 本机 URL 校验规则

`command_streak` 的**第一步**是 `validate_target_url(args.url)`
（`healthcheck.py:867`；函数定义 `healthcheck.py:394-444`），与 `check`
完全共用同一套本机地址规则，只接受
`http://127.0.0.1:<显式端口>[/path][?query]`，依次拒绝：

| 规则 | 代码位置 |
|---|---|
| 空值 / 非字符串 | `healthcheck.py:396-397` |
| 含空白或控制字符 | `healthcheck.py:399-400` |
| 结构无法解析（如未配对方括号） | `healthcheck.py:402-407` |
| 主机地址无法解析 | `healthcheck.py:409-414` |
| 协议不是 `http` | `healthcheck.py:416-417` |
| 主机不是 `127.0.0.1`（`ALLOWED_HOST`，`healthcheck.py:32`） | `healthcheck.py:418-419` |
| 端口非法、超出 1-65535 或未显式指定 | `healthcheck.py:420-428` |
| 含用户信息（userinfo） | `healthcheck.py:429-430` |
| 含片段（原始 `#`，不论内容、不论位于路径后还是查询后） | `healthcheck.py:431-436` |

校验只判定输入合法性；**匹配时仍使用命令行原始字符串**，不做任何规范化
（`healthcheck.py:865-866` 注释明确此点）。`streak` 不使用校验返回的
port/target——它不发请求，返回值被丢弃。

## 3. 阈值校验规则

URL 合法后，第二步是 `validate_threshold(args.threshold)`
（`healthcheck.py:873`；函数定义 `healthcheck.py:316-361`）：

- **省略**（`None`）：返回 `None`，不做阈值判断，输出保留原有三字段
  （`healthcheck.py:325-326`）。
- **提供时只接受纯 ASCII 十进制数字组成且数值大于零的文本，允许前导零**：
  形态由 `THRESHOLD_DIGITS_RE = re.compile(r"[0-9]+\Z")` 以 `fullmatch`
  判定（`healthcheck.py:73-75`、`333`）；通过后解除 Python 3.11+ 的整数
  字符串位数限制（`disable_int_digit_limit_for_threshold`，
  `healthcheck.py:301-313`，调用点 `347`），按 `int(text)` 解析
  （`348-355`），并要求 `> 0`（`356-360`）。返回正整数，**前导零不保留**，
  输出与比较均按整数进行。
- 以下均属**参数错误**，经 `die` 以退出码 `2` 拒绝（stdout 为空，stderr
  指出 `threshold` 及具体原因）：
  - 裸 `--threshold` 缺值（哨兵 `THRESHOLD_MISSING`，`327-331`）；
  - 空字符串（原因「值不能为空」，`334-335`）；
  - 前后带空白（原因「前后不允许有空白」，`336-337`）；
  - 负数、小数、正号、科学计数、十六进制、全角数字、非数字文本等一切
    非纯 ASCII 数字形态（`338-342`）；
  - 零及前导零的零（如 `0`、`000`）：形态合法但数值不大于零（`356-360`）。

**校验顺序：URL 校验先于阈值校验，两者均先于一切数据库访问**——URL 与阈值
同时非法时只报 URL；任一被拒绝时不会读取数据库、不创建目录（对应测试
`test_invalid_url_rejected_before_db_access`、
`test_threshold_invalid_values_rejected_before_db_access`，后者逐一覆盖
`""`、`0`、`000`、`-1`、`-0`、`1.5`、`.5`、`+3`、` 3`、`3 `、`\t3`、`3\n`、
`1 2`、`1e3`、`0x1`、`９`、`three` 及裸 `--threshold`）。

## 4. 只读打开数据库与历史表识别

参数全部合法后才接触数据库（`command_streak`，`healthcheck.py:879-916`）：

1. **只读打开**：`open_history_db_readonly(args.db)`
   （`healthcheck.py:486-504`，调用点 `879`）。路径（含父目录）不存在时
   返回 `None`，表示历史为空——不创建文件、目录，也不产生
   `-wal`/`-journal` 旁路文件（`494-495`）；路径指向目录（`496-497`）、
   打开失败（`503-504`）均以退出码 `2` 拒绝。正常路径以
   `?mode=ro` 只读 URI 打开（`500-502`），可读但不可写的文件也能查询。
2. **缺库快路径**：连接为 `None` 时直接输出 `latest_id: null`、次数 `0`
   并返回 `0`（`880-882`），见第 8 节。
3. **历史表名大小写兼容**：`resolve_checks_table`
   （`healthcheck.py:507-519`，调用点 `888`）按大小写不敏感在
   `sqlite_master` 中查找历史表——`CHECKS`、`Checks` 与 `checks` 视为同一
   历史表（SQLite 标识符本身大小写不敏感，这类异写表至多存在一个），查询
   时使用库中保存的实际表名（经 `quote_identifier` 加引号，
   `healthcheck.py:481-483`，调用点 `896`）。空库或仅有其他表时返回
   `None`，按空结果处理（`889-892`），不建表。
4. **缺字段即错误**：表存在但缺 `REQUIRED_COLUMNS`
   （`id, url, checked_at, elapsed_ms, status, http_status, reason`，
   `healthcheck.py:110-112`）中任意一个时，`ensure_checks_columns`
   （`healthcheck.py:466-478`，调用点 `895`）以退出码 `2` 报缺列，
   不补列、不重建表。字段名比较沿用 SQLite 的大小写不敏感语义。
5. **读取失败**：查询执行中的 SQLite 错误（如文件不是有效数据库）统一
   以退出码 `2` 拒绝（`913-916`）。

## 5. 连续段计算：按 id 倒序，遇首条 success 即止

### 5.1 查询：只取该目标的 (id, status)，无 LIMIT

`STREAK_SQL`（`healthcheck.py:141-146`，执行点 `899-901`）：

```sql
SELECT id, status
FROM {table}
WHERE url = ?
ORDER BY id DESC
```

- **目标按保存的 URL 原文精确匹配**：`WHERE url = ?` 绑定命令行原始字符
  串，不做任何规范化——带不带 query、中文原文与编码形式都是不同目标
  （对应测试 `test_url_exact_verbatim_match_no_normalization`、
  `test_unicode_url_echoed_verbatim`）。
- **其他目标不参与**：WHERE 在排序之前过滤，其他目标的记录（无论成败）
  根本不进入结果集，自然不会打断连续段。
- **按 id 倒序、不带 LIMIT**：连续段先后只由 id 大小定义，`checked_at`
  不参与排序（即使时间乱序也按 id 判定，对应测试
  `test_ordering_by_id_not_checked_at`）；次数**不受 `recent` 默认五条
  限制**（对应测试 `test_not_capped_by_recent_default_limit_five`：
  12 连败返回 12）。
- `reason`、`http_status` 两列根本不在 SELECT 中，**不参与判断**——保存
  为 `failure` 但 `http_status` 为 200、`reason` 为 `ok` 的记录仍计入
  连续失败（对应测试 `test_only_saved_status_matters_not_reason_or_http_status`）。

### 5.2 逐条判定：failure 累计，success 停止，其他取值拒绝

`count_consecutive_failures`（`healthcheck.py:822-842`，调用点 `910`）
自最大 id 起逐条遍历：

- `status == "failure"`：计数加一（`833-834`）；
- `status == "success"`：**立即停止**，更旧的记录不再遍历（`835-836`）；
- 其他取值：经 `die` 以退出码 `2` 拒绝并**指出记录 id**
  （`838-841`）——即使此前累计次数已达到阈值也照样拒绝（对应测试
  `test_threshold_unknown_status_rejected_even_after_reached`）；
  而**比首条 success 更旧的记录不会被遍历到**，其非法 status 不影响结果
  （对应测试 `test_stops_at_first_success_and_ignores_older_rows`、
  `test_threshold_unknown_status_below_first_success_unseen`）。

`latest_id` 取结果集第一行的 id（即该目标最大 id，`healthcheck.py:906`）。
最新一条为 success 时次数为 0 但 `latest_id` 照常返回（对应测试
`test_latest_success_gives_zero_but_keeps_latest_id`）；全部 failure 时
统计全部（`test_all_failures_counts_all`）。

## 6. 输出格式：单行紧凑 JSON，三字段或五字段

`render_streak_result`（`healthcheck.py:845-861`，调用点 `918-920`）输出
**一行紧凑 JSON 对象加一个换行**（`json.dumps(..., separators=(",", ":"))`，
`861`），字段名与顺序固定：

| 字段 | 含义 | 何时出现 |
|---|---|---|
| `url` | 命令行输入原文，不做任何规范化 | 总是 |
| `latest_id` | 该目标最大 id；无记录时为 `null` | 总是 |
| `consecutive_failures` | 连续失败次数（整数） | 总是 |
| `threshold` | 阈值（**整数**，前导零不保留） | 仅提供 `--threshold` 时 |
| `threshold_reached` | `consecutive_failures >= threshold`（**布尔**） | 仅提供 `--threshold` 时 |

省略 `--threshold` 时恰为原有三字段；提供时追加后两字段（`858-860`）。
**正常查询（含空结果）均退出 `0` 且 stderr 为空**；阈值判断为「次数大于
或等于阈值」为 `true`（`860`）。

## 7. 固定样本与本地 SQLite 准备

### 7.1 固定样本（复用 `test_streak.py`，4 行）

样本与 `test_streak.py` 的 `test_spec_example_interleaved_other_target`
（`test_streak.py:148-160`）逐字段一致：目标 A
`http://127.0.0.1:8765/health` 占 id 1、2、4，另一目标 B
`http://127.0.0.1:8765/ready` 占 id 3；`elapsed_ms` 均为 0（夹具
`build_db` 固定写入，`test_streak.py:57-68`），`checked_at` 由辅助函数
`R` 按 id 生成（`test_streak.py:71-75`）：

| id | url | checked_at | elapsed_ms | status | http_status | reason |
|----|-----|-----------|-----------|--------|-------------|--------|
| 1 | `http://127.0.0.1:8765/health` | `2026-10-04T00:00:01.000000+00:00` | 0 | success | 200 | ok |
| 2 | `http://127.0.0.1:8765/health` | `2026-10-04T00:00:02.000000+00:00` | 0 | failure | null | connection_error |
| 3 | `http://127.0.0.1:8765/ready` | `2026-10-04T00:00:03.000000+00:00` | 0 | success | 200 | ok |
| 4 | `http://127.0.0.1:8765/health` | `2026-10-04T00:00:04.000000+00:00` | 0 | failure | 500 | http_status |

关键安排：目标 A 按 id 倒序为 4（failure）、2（failure）、1（success），
连续失败 2 次；id 3 是另一目标的成功，被 `WHERE url = ?` 排除，**不打断**
连续段（见第 7.2 节要点）。

### 7.2 可复现的本地样本准备

只用 Python 3 标准库直接建库，**不发任何网络请求**；与测试夹具 `build_db`
写入的内容等价。在项目目录执行：

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
    status TEXT,
    http_status INTEGER,
    reason TEXT NOT NULL
)
""")
conn.executemany(
    "INSERT INTO checks (id, url, checked_at, elapsed_ms, status, "
    "http_status, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
    [
        (1, "http://127.0.0.1:8765/health",
         "2026-10-04T00:00:01.000000+00:00", 0, "success", 200, "ok"),
        (2, "http://127.0.0.1:8765/health",
         "2026-10-04T00:00:02.000000+00:00", 0, "failure", None,
         "connection_error"),
        (3, "http://127.0.0.1:8765/ready",
         "2026-10-04T00:00:03.000000+00:00", 0, "success", 200, "ok"),
        (4, "http://127.0.0.1:8765/health",
         "2026-10-04T00:00:04.000000+00:00", 0, "failure", 500,
         "http_status"),
    ],
)
conn.commit()
conn.close()
EOF
```

表结构即 `test_streak.py` 的 `SCHEMA`（`test_streak.py:37-47`）：与产品
代码的 `CREATE_TABLE_SQL`（`healthcheck.py:90-101`）相比仅去掉了
`status`/`reason` 的 CHECK 约束，以便测试夹具能写入非法 status 样本；
本样本四行全部满足产品约束，两种结构下查询结果一致。准备完成后即可照
第 8 节命令复现。**第 8 节给出的输出均为该样本上的源码推导预期结果**，
不是某次实际运行的转存。

## 8. 验收示例（预期结果，源码推导）

### 8.1 省略阈值：三字段输出

```sh
python healthcheck.py --db monitor.sqlite streak --url http://127.0.0.1:8765/health
```

预期输出（单行 JSON；退出码 `0`，stderr 为空）：

```json
{"url":"http://127.0.0.1:8765/health","latest_id":4,"consecutive_failures":2}
```

### 8.2 传入 `--threshold 002`：追加两字段，判断为 true

```sh
python healthcheck.py --db monitor.sqlite streak --url http://127.0.0.1:8765/health --threshold 002
```

预期输出（单行 JSON；退出码 `0`，stderr 为空）：

```json
{"url":"http://127.0.0.1:8765/health","latest_id":4,"consecutive_failures":2,"threshold":2,"threshold_reached":true}
```

要点（两次查询共用）：

- `WHERE url = ?` 只命中 id 1、2、4 三行，按 id 倒序为 4、2、1；
  `latest_id` 为最大 id **4**；
- 自 id 4 起：4 是 failure（计 1）、2 是 failure（计 2）、1 是 success
  即停止，`consecutive_failures` 为 **2**；
- **id 3 的成功为何不打断连续段**：id 3 的 `url` 是
  `http://127.0.0.1:8765/ready`，与目标原文不相等，在 SQL 的 WHERE 阶段
  就被排除，根本不在遍历序列中——连续段只在同一目标的记录内定义；
- 第二次查询中 `002` 是合法阈值（纯 ASCII 数字、允许前导零），输出按整数
  表示为 `"threshold":2`（原文的前导零不出现）；`2 >= 2` 成立，故
  `threshold_reached` 为 `true`；
- id 2 的 `http_status` 为 null、`reason` 为 `connection_error`，id 4 的
  `http_status` 为 500、`reason` 为 `http_status`——这些字段都不参与
  判断，计入连续失败只凭保存的 `status` 为 `failure`。

## 9. 空结果与错误的区分

### 9.1 空结果：退出码 0，`latest_id` 为 null、次数为 0

以下情形**都不是错误**，退出码均为 `0`、stderr 为空，输出
`{"url":<原文>,"latest_id":null,"consecutive_failures":0}`；提供阈值时
追加 `"threshold":<整数>,"threshold_reached":false`（`0 >= 正整数` 不
成立）：

| 情形 | 代码位置 |
|---|---|
| 数据库文件不存在（父目录也不存在也一样；不创建任何东西） | 缺库快路径，`healthcheck.py:879-882`（`open_history_db_readonly` 返回 `None`，`494-495`） |
| 有效数据库但没有历史表（空库或仅有其他表） | 表缺失分支置 `latest_id = None`、次数 0，不建表，`healthcheck.py:888-892` |
| 历史表存在但为空表，或该目标无匹配记录 | 查询返回 0 行分支，`healthcheck.py:902-904` |

对应测试：`test_no_matching_record`、
`test_missing_db_and_missing_parent_creates_nothing`、
`test_empty_db_and_other_table_only`、`test_history_table_exists_but_empty`、
`test_threshold_empty_results_false`。

### 9.2 错误：退出码 2，stdout 为空，原因写 stderr

| 错误 | 代码位置 |
|---|---|
| 缺 `--db` / 缺 `--url` / 未知选项（含 recent 专属选项） | argparse，`healthcheck.py:929`、`1019-1026` |
| 非法 URL（规则见第 2 节） | `validate_target_url`，`healthcheck.py:394-444`，调用点 `867` |
| 非法阈值（规则见第 3 节） | `validate_threshold`，`healthcheck.py:316-361`，调用点 `873` |
| 数据库路径是目录 | `open_history_db_readonly`，`healthcheck.py:496-497` |
| 文件不是有效 SQLite 数据库、无读权限等打开/读取失败 | `healthcheck.py:503-504`、`913-916` |
| 历史表缺必需字段 | `ensure_checks_columns`，`healthcheck.py:466-478`，调用点 `895` |
| 连续段内出现 `success`/`failure` 之外的 status（指出记录 id，即使此前已达阈值） | `count_consecutive_failures`，`healthcheck.py:838-841` |

所有参数与数据库错误统一经 `die`（`healthcheck.py:148-151`）出口：写
stderr（`healthcheck: error: ...`）、退出码 `2`、stdout 保持为空、无
Python 回溯。**报告顺序固定：URL 校验先于阈值校验，两者先于数据库访问**
（`command_streak` 的 `867` → `873` → `879`）。比首条 success 更旧的
记录即使有非法 status 也不构成错误（不会被遍历，见第 5.2 节）。

## 10. 只读、无副作用保证

- **不发送网络请求**：`streak` 路径不调用 `probe_once`
  （`healthcheck.py:539-563`），URL 仅做本地合法性校验与字符串匹配。
- **不创建文件或目录**：数据库/父目录缺失时直接输出空结果
  （`healthcheck.py:879-882`），不调用会建库建表的 `open_database`。
- **不创建表、不改动已有记录**：库以只读 URI（`?mode=ro`）打开
  （`healthcheck.py:500-502`），全程只有 `SELECT`（含对 `sqlite_master`
  的查询）；无历史表时仅置空结果，不补建表。
- **可读但不可写的数据库仍可查询**：只读 URI 不需要写权限，也不产生
  `-wal`/`-journal` 旁路文件（对应测试
  `test_readonly_db_still_queries_and_modifies_nothing`、
  `test_threshold_with_case_table_and_readonly_db`）。
- **不生成告警事件**：`streak` 的唯一输出是 stdout 上的一行 JSON；
  阈值判断结果只体现在 `threshold_reached` 字段中，程序不据此触发任何
  通知、写入或后续动作。
- 回归测试 `test_streak.py` 的多个用例用 `snapshot` 逐一比对查询前后的
  目录文件集合、表结构与全部记录内容，确认 streak 查询严格只读
  （如 `test_threshold_does_not_change_query_readonlyness`）。

## 11. 结论到源码位置的对照

| 结论 | 函数 / 常量 | 位置（`healthcheck.py`） |
|---|---|---|
| streak 处理总流程 | `command_streak` | `864-921` |
| `--db`、`--url` 必填；`--threshold` 可选（裸参数注入哨兵） | `build_parser` streak 子解析器 / `THRESHOLD_MISSING` | `929`、`1015-1040` / `69-71` |
| 本机 URL 规则（仅 `http://127.0.0.1:端口/...`），校验不做规范化 | `validate_target_url` / `ALLOWED_HOST` | `394-444` / `32`，调用点 `867` |
| 阈值只接受大于零的纯 ASCII 数字文本（允许前导零，输出为整数） | `validate_threshold` / `THRESHOLD_DIGITS_RE` | `316-361` / `73-75`，调用点 `873` |
| 解除超长数字串的整数解析限制 | `disable_int_digit_limit_for_threshold` | `301-313`，调用点 `347` |
| 校验顺序：URL → 阈值 → 数据库访问 | `command_streak` 前三步 | `867`、`873`、`879` |
| 只读打开；缺库（含父目录缺失）返回 None 视为空 | `open_history_db_readonly` | `486-504`（缺库 `494-495`，目录 `496-497`，只读 URI `500-502`） |
| 历史表名大小写不敏感识别，用库中实际表名查询 | `resolve_checks_table` / `quote_identifier` | `507-519` / `481-483`，调用点 `888`、`896` |
| 缺必需字段以退出码 2 报缺列 | `ensure_checks_columns` / `REQUIRED_COLUMNS` | `466-478` / `110-112`，调用点 `895` |
| 目标按 URL 原文精确匹配；按 id 倒序；无 LIMIT（不受 recent 五条限制）；只取 (id, status) | `STREAK_SQL` | `141-146`，执行点 `899-901` |
| 连续段：failure 累计、首条 success 即止、其他 status 退出码 2 并指出 id | `count_consecutive_failures` / `STATUS_SUCCESS` / `STATUS_FAILURE` | `822-842` / `41` / `42`，调用点 `910` |
| `latest_id` 为该目标最大 id | 取首行 id | `906` |
| 空结果：缺库 / 无历史表 / 无匹配记录 → null + 0 | 三个空结果分支 | `880-882` / `889-892` / `902-904` |
| 三字段 / 五字段单行紧凑 JSON；`threshold_reached` 为「次数 >= 阈值」 | `render_streak_result` | `845-861`（阈值分支 `858-860`），调用点 `918-920` |
| 错误统一出口（退出码 2、stderr、stdout 空） | `die` | `148-151` |
| 读取失败以退出码 2 拒绝 | `except sqlite3.Error` | `913-916` |
