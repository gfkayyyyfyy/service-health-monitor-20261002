# `check` 检查流程说明（参数校验 → 数据库准备 → 单次探测 → 记录提交 → JSON 输出）

本说明描述 `healthcheck.py` **当前已实现**的 `check` 子命令的完整路径：从公开
命令行参数进入，沿「参数校验 → 数据库准备 → 发送一次 GET → 归类探测结果 →
提交检查记录 → 单行 JSON 输出」讲清每一步，并标注关键结论对应的函数或常量
（行号以当前 `healthcheck.py` 为准）。用途是核对**一次检查的结果与耗时含义**：
四个字段（`status` / `http_status` / `reason` / `elapsed_ms`）如何确定、
`checked_at` 在什么时刻生成、记录何时才算落库、各退出码分别表示什么。

- 入口函数：`main`（`healthcheck.py:787-790`）→ `command_check`
  （`healthcheck.py:474-516`）。
- **本文不新增、不修改任何行为**：公开入口仍是
  `python healthcheck.py --db <库> check --url <URL> [--timeout <秒>]`；
  数据库结构仍是 `CREATE_TABLE_SQL`（`healthcheck.py:82-93`）定义的七字段
  `checks` 表；`recent` 的既有查询行为完全不受影响（其流程见
  `RECENT_QUERY_FLOW.md`）。

> 文中所有命令输出块均为**预期结果**（依据当前实现逐条推导，核对方式见
> `test_healthcheck.py`、`test_check_http_status.py`、
> `test_check_timeout.py`、`test_check_connection_error.py`、
> `test_check_unicode_url.py`、`test_check_malformed_url.py`、
> `test_check_timeout_validation.py` 与 `test_check_missing_columns.py`），
> 表示在对应本地环境下运行时**应当**得到的 stdout / 退出码 / 落库变化；
> 它们不是某次固定运行的转存，其中 `checked_at` 与 `elapsed_ms` 只给判定
> 口径、不伪造精确值。第 7 节给出两个可照做的本地复现实例。

---

## 1. 公开参数

命令形式（参数定义见 `build_parser` 中 check 子解析器，
`healthcheck.py:708-716`；全局 `--db` 见 `healthcheck.py:705`）：

```sh
python healthcheck.py --db <数据库路径> check \
    --url <本机HTTP目标URL> [--timeout <有限正数秒>]
```

| 参数 | 是否必填 | 默认 | 约束与语义 | 代码位置 |
|---|---|---|---|---|
| `--db` | 必填（全局） | 无 | SQLite 数据库文件路径；文件不存在时**由 check 创建**，但其**父目录必须已存在** | `healthcheck.py:705` |
| `--url` | 必填 | 无 | 仅 `http://127.0.0.1:<显式端口>[/path][?query]`，规则见第 1.1 节 | `healthcheck.py:709`，校验 `339-389` |
| `--timeout` | 可选 | `1.0`（`DEFAULT_TIMEOUT`，`healthcheck.py:30`） | **有限正数秒**：必须能解析为 float，且非 NaN、非无穷、严格大于 0 | `positive_timeout`，`healthcheck.py:141-148`；参数定义 `710-715` |

### 1.1 `--url` 的本机规则

`--url` 由 `validate_target_url`（`healthcheck.py:339-389`）校验，**只接受
`http://127.0.0.1:<显式端口>` 形式的本机 HTTP 目标**（允许路径与查询）：

- 必须是非空字符串，含**空白或控制字符**即拒绝（`healthcheck.py:341-345`）；
- 结构无法解析（如未配对方括号）、主机地址无法解析（如 `http://[]/`）即拒绝；
- 协议必须是 `http`（`healthcheck.py:361-362`），主机名必须**逐字等于**
  `ALLOWED_HOST = "127.0.0.1"`（`healthcheck.py:29, 363-364`）——`localhost`、
  `127.1`、其他回环/本机地址、任何非本机地址一律拒绝；
- 必须**显式指定端口**，且端口在 `1-65535` 之间
  （`healthcheck.py:365-373`）；
- 不接受用户信息（userinfo，`healthcheck.py:374-375`）；
- **原始字符串中只要出现 `#` 即按片段（fragment）拒绝**，即使是空片段
  （`healthcheck.py:376-381`）；注意 `%23` 是普通百分号编码文本、不是原始
  `#`，可以合法出现在路径或查询中。

校验通过后，函数返回 `(port, target)`（`healthcheck.py:383-389`）：`target`
是发往服务器的请求目标——路径缺省为 `/`，再依次拼上 `;params` 与
`?query`（本工具不接受片段，故无片段部分）。**校验只判定合法性，不做任何
规范化**：落库与输出使用的始终是命令行上的原始 URL（见第 4.3 节）。

### 1.2 `--timeout` 的有限正数规则

`--timeout` 由 `positive_timeout`（`healthcheck.py:141-148`）在 argparse
阶段转换：

- 不能解析为数字（如 `abc`、空值）→ `ArgumentTypeError`；
- `NaN`、`inf`、`-inf`（`math.isfinite` 为假）、`0`、负数 →
  `ArgumentTypeError`（文案为「超时必须是有限正数秒」）；
- 合法时作为 socket 级超时传给 `HTTPConnection`
  （`healthcheck.py:448`），同时约束建立连接与等待响应。

argparse 参数错误（含非法 `--timeout`、缺少必填的 `--db`/`--url`、未知
子命令或参数）由 argparse 自身处理：**退出码 2、stdout 为空、原因写入
stderr**，发生在 `command_check` 执行之前，因此不会打开数据库、更不会发
请求。

---

## 2. 端到端流程

`main` 解析参数后调用 `args.handler`，对 `check` 即 `command_check`
（`healthcheck.py:474-516`）。步骤严格按以下顺序发生：

1. **参数解析与 `--timeout` 转换**（argparse 阶段，先于处理函数）。
   非法 `--timeout`、缺必填参数等在此即以退出码 `2` 结束（见第 1.2 节）。

2. **校验 `--url`**（`healthcheck.py:475` → `validate_target_url`）。
   非法 URL 经 `die`（`healthcheck.py:135-138`）写 stderr
   （`healthcheck: error: …`）并以退出码 `2` 结束，**stdout 为空**。
   这一步先于一切数据库访问与网络操作：**不打开/创建数据库、不发请求、
   不新增检查记录**。合法时得到 `(port, target)`。

3. **探测前准备数据库**（`healthcheck.py:478` → `open_database`，
   `healthcheck.py:392-408`）。下列任一情形都在**发送请求之前**经 `die`
   以退出码 `2` 结束（stdout 为空、原因写 stderr、不发请求、不新增记录）：

   1. **数据库父目录不存在**：对 `os.path.abspath(db_path)` 的父目录做
      `os.path.isdir` 判断，非目录即拒绝
      （`healthcheck.py:398-400`，「数据库父目录不存在: …」）。
      工具不会替用户创建任何目录。
   2. **无法打开或初始化数据库**：`sqlite3.connect`、`CREATE TABLE` 执行或
      `commit` 抛出任意 `sqlite3.Error`（如路径实际是目录、权限不足、文件
      不是有效 SQLite 数据库）时统一拒绝
      （`healthcheck.py:401-407`，「无法打开或初始化数据库 …」）。
   3. **已有 `checks` 表缺少字段**：建表语句是
      `CREATE TABLE IF NOT EXISTS`（`healthcheck.py:403`），对**已存在**的
      表是空操作；随后 `ensure_checks_columns`
      （`healthcheck.py:411-423`）通过 `PRAGMA table_info(checks)` 核对
      `REQUIRED_COLUMNS`（`healthcheck.py:102-104`：`id`、`url`、
      `checked_at`、`elapsed_ms`、`status`、`http_status`、`reason`）
      是否齐全，缺任意一个即报「checks 表缺少字段: …」退出码 `2`。
      **缺字段时不补列（不执行 ALTER TABLE）、不重建表（不 DROP/不 CREATE
      覆盖）**，原有表结构与数据保持不变；列顺序不同或存在额外列均不构成
      问题，字段名比较沿用 SQLite 的大小写不敏感语义。

   数据库文件本身不存在、但父目录存在时，这一步会创建文件并建好七字段
   `checks` 表（这是**全工具唯一的建库建表路径**；`recent` 从不调用
   `open_database`，见 `RECENT_QUERY_FLOW.md` 第 11 节）。

4. **发送一次 GET**（`healthcheck.py:482-484` → `probe_once`，
   `healthcheck.py:443-467`）。合法输入**只发一次 GET**：
   `http.client` 不自动跟随重定向，故 3xx 响应也只是一个普通的「收到了
   响应」，不会产生第二次请求；连接在 `finally` 中关闭
   （`healthcheck.py:466-467`）。请求目标在发送前经
   `encode_request_target`（`healthcheck.py:426-440`）编码，规则见第 4.3
   节；计时起止与 `elapsed_ms` 口径见第 5 节。

5. **归类探测结果**（`healthcheck.py:456-465`）。按第 3 节的四行对照表
   确定 `(status, http_status, reason, elapsed_ms)`。

6. **探测结束后生成 `checked_at`**（`healthcheck.py:485` →
   `utc_now_iso`，`healthcheck.py:470-471`）。在 `probe_once` 返回**之后**
   才调用 `datetime.now(timezone.utc).isoformat()`，得到带 UTC 偏移的
   ISO 8601 字符串（形如 `2026-10-04T22:59:27.191834+00:00`）。它标记
   **本次探测结束**（响应头已收到或异常已抛出）的时刻，而不是命令启动或
   记录写入完成的时刻。

7. **提交检查记录**（`healthcheck.py:495-510`）。用 `INSERT_SQL`
   （`healthcheck.py:95-98`）绑定**原始 URL**、刚生成的 `checked_at`、
   `elapsed_ms`、`status`、`http_status`、`reason` 执行插入并 `commit()`；
   `cur.lastrowid` 作为记录 `id`（自增主键，新库第一条为 1）。插入或提交
   抛 `sqlite3.Error` 时经 `die` 报「写入检查记录失败: …」以**退出码 2**
   结束、**stdout 为空**；此时探测已经发生且无法撤回，但事务未提交
   （`finally` 中关闭连接，未提交的插入回滚），**数据库中没有本次记录、
   也没有 JSON 输出**。

8. **提交成功后才输出 JSON**（`healthcheck.py:514-516`）。`print` 在
   `commit()` 成功之后执行：单行紧凑 JSON（`separators=(",", ":")`、
   `ensure_ascii=False`，故非 ASCII 原样出现），末尾一个换行；字段集合与
   键顺序固定为 `id, url, checked_at, elapsed_ms, status, http_status,
   reason`（`healthcheck.py:486-494`）。返回值由探测状态决定：
   **探测成功退出 0，探测失败（已正常落库）退出 1**
   （`healthcheck.py:516`）；两种情况下 stderr 均为空。

---

## 3. 探测结果的四行归类

`probe_once`（`healthcheck.py:443-467`）对一次 GET 的所有可能结局做如下
归类；`status`、`reason` 的取值分别来自常量 `STATUS_SUCCESS` /
`STATUS_FAILURE`（`healthcheck.py:38-39`）与 `REASON_OK` /
`REASON_HTTP_STATUS` / `REASON_CONNECTION_ERROR` / `REASON_TIMEOUT`
（`healthcheck.py:33-36`）：

| 探测结局 | 判定位置 | `status` | `reason` | `http_status` | 退出码 |
|---|---|---|---|---|---|
| 收到响应且状态码为 **2xx**（`200 <= status < 300`） | `healthcheck.py:456-457` | `success` | `ok` | 该状态码（整数） | **0** |
| 收到响应但状态码**不是 2xx**（含不跳转的 3xx、4xx、5xx） | `healthcheck.py:458` | `failure` | `http_status` | 该状态码（整数） | **1** |
| **超时**：建立连接或等待响应超过 `--timeout`（`TimeoutError`） | `healthcheck.py:459-461` | `failure` | `timeout` | **`null`** | **1** |
| **其他连接错误**：连接被拒/被重置、对端未发响应即断开、响应畸形等（`OSError`、`http.client.HTTPException`） | `healthcheck.py:462-465` | `failure` | `connection_error` | **`null`** | **1** |

要点：

- **只有 2xx 是 success**；3xx 因不跟随重定向而归入 failure/http_status。
- 后两类网络异常**拿不到 HTTP 状态码**，故 `http_status` 落库与输出均为
  SQL/JSON 的 `null`（INSERT 绑定 Python `None`，`healthcheck.py:450,
  461, 465`）。
- **连接被拒（端口无人监听）属于 `connection_error`，不是 `timeout`**：
  拒连通常立即返回错误，不会等到 `--timeout`。第 7.2 节示例即此情形。
- 探测失败（上表后三行）只要记录提交成功，退出码就是 **1** 且仍输出一行
  JSON；退出码 **2** 只属于参数/数据库错误与写入失败（第 6 节）。

---

## 4. 请求的发送方式与 URL 的三种形态

同一次检查中 URL 有三种形态，务必区分：

1. **原始输入**：命令行上的 `args.url` 逐字字符串——**落库的 `url` 列与
   JSON 输出的 `url` 字段都是它**（`healthcheck.py:489, 499`）。
2. **请求目标 `target`**：`validate_target_url` 从原始 URL 取出的
   `path[;params][?query]`（`healthcheck.py:383-389`），用于 HTTP 请求行。
3. **线上字节**：`target` 经 `encode_request_target` 编码后的 ASCII 字符串
   （`healthcheck.py:482-484`），是服务器实际收到的请求目标。

### 4.1 只发一次 GET、不跟随重定向

`probe_once` 内只有一次 `conn.request("GET", target)` 与一次
`conn.getresponse()`（`healthcheck.py:452-453`），没有任何重试或重定向
跟随；`http.client` 本身也不自动跟随 `Location`。回归用例
（如 `test_check_http_status.py`、`test_check_unicode_url.py`）通过记录
服务器收到的请求列表断言「服务恰好收到一次 GET」。

### 4.2 主机、端口与超时固定取自校验结果

连接对象固定为 `HTTPConnection(ALLOWED_HOST, port, timeout=timeout)`
（`healthcheck.py:448`），即主机恒为 `127.0.0.1`、端口与超时都来自前面的
合法参数；工具不自行更换主机、端口或补默认路径之外的任何内容。

### 4.3 非 ASCII 按 UTF-8 百分号编码上线；既有编码与原始 URL 保留

`encode_request_target`（`healthcheck.py:426-440`）逐字符处理请求目标：

- **仅**对非 ASCII 字符（`ord(ch) > 127`）按其 **UTF-8 字节**逐字节转成
  **大写十六进制**百分号编码（`f"%{b:02X}"`）。例如「健」→
  `%E5%81%A5`、「康」→ `%E5%BA%B7`；
- 所有 ASCII 字符**原样保留**，包括已有的百分号编码（**不重复编码**：
  `%23` 不会变成 `%2523`，原有大小写也不动）、路径分隔符 `/`、查询的
  `?`、`=`、`&`、`;`、以及查询中的 `+`（不改成 `%20`）；
- 该函数**只作用于发送时的请求目标**：数据库 `url` 列与 stdout JSON 中
  保存/输出的仍是**含中文等原字符的原始 URL**
  （`healthcheck.py:480-484` 注释及 `489, 499` 的绑定值）。

例如原始 URL 为 `http://127.0.0.1:8765/?q=健康%23` 时：

- 请求目标（`target`）为 `/?q=健康%23`；
- 服务器实际收到的请求行为
  `GET /?q=%E5%81%A5%E5%BA%B7%23 HTTP/1.1`（「健康」被编码，既有
  `%23` 原样保留）；
- 落库与输出的 `url` 仍是 `http://127.0.0.1:8765/?q=健康%23`。

更复杂的混合样例（中文路径段、原有小写 `%2f`、`%23`、查询里的 `+` 与
参数顺序）见 `test_check_unicode_url.py` 顶部的 `RAW_PATH_QUERY` /
`EXPECTED_TARGET`。

---

## 5. `elapsed_ms` 与 `checked_at` 的含义

### 5.1 `elapsed_ms`：到响应头或异常为止的探测毫秒数

- 计时起点：`start = time.monotonic()`（`healthcheck.py:449`），位于
  `HTTPConnection` 构造之后、`conn.request` 之前。http.client 延迟到发
  请求时才真正建立 TCP 连接，因此连接建立的耗时计入；而连接对象构造、
  参数校验、数据库打开/建表都在起点之前，**不计入**。
- 计时终点有两个（之后立即
  `max(0, round((time.monotonic() - start) * 1000))`，
  `healthcheck.py:455, 460, 464`）：
  - 收到响应时：`getresponse()` 返回、即**响应状态行与响应头已解析完成**
    的时刻（`healthcheck.py:453-455`）；
  - 发生异常时：进入 `TimeoutError` / `OSError` / `HTTPException` 处理
    分支的时刻（`healthcheck.py:459-465`），在连接关闭之前完成取值。
- 因此 `elapsed_ms`：
  - **不包含响应体（body）的完整读取**——代码在拿到响应头后立即读
    `resp.status` 并返回，从不读取响应体；
  - **不是覆盖整条命令的总耗时**——不包含 `checked_at` 生成、SQLite
    插入与 `commit`、JSON 序列化与输出、进程启动与 argparse/数据库准备；
  - 是**非负整数毫秒**：`round(...)` 取整、`max(0, …)` 兜底为非负
    （Python 中减法不会为负，兜底仅为防御），库表亦有
    `CHECK (elapsed_ms >= 0)` 约束（`healthcheck.py:87`）。

### 5.2 `checked_at`：探测结束后生成的 UTC 时刻

- 生成点在 `probe_once` 返回**之后、INSERT 之前**
  （`healthcheck.py:485`），由 `utc_now_iso`
  （`healthcheck.py:470-471`）输出
  `datetime.now(timezone.utc).isoformat()`；
- 形态为带时区的 UTC ISO 8601：`YYYY-MM-DDTHH:MM:SS.ffffff+00:00`
  （offset 恒为 `+00:00`；微秒位由 `isoformat()` 给出）。它与 `recent`
  的 `--since`/`--until` 接受的严格 UTC 格式同族（可被
  `parse_utc_timestamp` 按同一时刻解析，见 `RECENT_QUERY_FLOW.md`
  第 1.4 节）。

---

## 6. 三类退出码与「拒绝即无副作用」汇总

| 情形 | 退出码 | stdout | stderr | 是否发请求 | 是否新增记录 |
|---|---|---|---|---|---|
| 探测到 2xx 并提交成功 | **0** | 一行 success JSON | 空 | 一次 GET | 新增 1 条 success |
| 探测到非 2xx / 超时 / 连接错误，并提交成功 | **1** | 一行 failure JSON | 空 | 一次 GET（或尝试连接） | 新增 1 条 failure |
| argparse 错误（非法 `--timeout`、缺 `--db`/`--url`、未知参数等） | **2** | **空** | argparse 错误说明 | 否 | 否 |
| `--url` 非法（`validate_target_url` → `die`） | **2** | **空** | `healthcheck: error: …` | 否 | 否 |
| DB 父目录不存在（`open_database` → `die`） | **2** | **空** | 「数据库父目录不存在: …」 | 否 | 否（也不建目录/文件） |
| DB 无法打开/初始化（含路径是目录、权限、坏文件） | **2** | **空** | 「无法打开或初始化数据库 …」 | 否 | 否 |
| 已有 `checks` 表缺字段（`ensure_checks_columns` → `die`） | **2** | **空** | 「checks 表缺少字段: …」 | 否 | **不补列、不重建表** |
| 探测成功结束但 INSERT/`commit` 失败（`healthcheck.py:509-510`） | **2** | **空** | 「写入检查记录失败: …」 | **已发出**（不可撤回） | 否（事务回滚，无 JSON 输出） |

统一错误出口是 `die`（`healthcheck.py:135-138`）：打印
`healthcheck: error: <原因>` 到 stderr 并 `raise SystemExit(2)`。所有退出码
2 的路径 stdout 都保持为空；其中**探测前**的三类拒绝（参数、URL、数据库）
保证不发请求、不新增检查记录。

---

## 7. 两个可复核的本地示例（共用一个全新数据库）

两个示例**共用同一个全新数据库** `demo/checkflow.sqlite`：其父目录 `demo/`
事先创建好（必须已存在），数据库文件本身在示例一首次运行 check 时才创建。
检查目标统一为：

```
http://127.0.0.1:8765/?q=健康%23
```

该 URL 合法（http、主机 `127.0.0.1`、显式端口 8765、无原始 `#`；`%23` 是
普通百分号编码文本）。示例一在「立即返回 200」的本机服务存活时运行，
得到 **id=1 的成功记录**；示例二在**服务停止且 8765 端口无人监听**时检查
**同一 URL**，得到 **id=2 的连接失败记录**。

> 以下 JSON 中 `checked_at` 与 `elapsed_ms` 以占位符表示：这两个值每次
> 运行都不同，**只按第 7.4 节的判定口径核对，不伪造精确值**；其余字段、
> 字段集合、请求目标、退出码与落库变化都是确定的。所有结论均为**依据
> 当前源码推导的预期结果**（第 7.5 节给出实际复核命令）。

### 7.1 准备：父目录、立即返回 200 的服务

在项目目录（含 `healthcheck.py`）执行：

```sh
# 1) 新建父目录（数据库文件此刻尚不存在）
mkdir -p demo

# 2) 终端 A：启动一个对任意 GET 立即返回 200、并回显请求目标的本机服务
python3 - <<'EOF'
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # 回显服务器实际收到的请求目标（path?query），便于核对线上字节
        print(f"server received request target: {self.path}", flush=True)
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # 抑制默认访问日志，只保留上面的请求目标回显

ThreadingHTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
EOF
```

### 7.2 示例一：服务存活时检查（id=1，success）

终端 B 执行**完整的 check 调用**（显式写出默认超时 1 秒；省略
`--timeout` 等价于 `--timeout 1`）：

```sh
python3 healthcheck.py --db demo/checkflow.sqlite check \
    --url 'http://127.0.0.1:8765/?q=健康%23' --timeout 1
```

确定结论：

- **发送的请求目标**：只发出一次 GET，请求行为
  `GET /?q=%E5%81%A5%E5%BA%B7%23 HTTP/1.1`——「健康」按 UTF-8 变为
  `%E5%81%A5%E5%BA%B7`，既有 `%23` 原样保留（终端 A 打印
  `server received request target: /?q=%E5%81%A5%E5%BA%B7%23`）；
- **stdout**：恰为一行 JSON，七个固定字段、固定键顺序、中文 URL 原样
  （`ensure_ascii=False`）：

  ```json
  {"id":1,"url":"http://127.0.0.1:8765/?q=健康%23","checked_at":"<探测结束后的UTC时刻>","elapsed_ms":<非负整数>,"status":"success","http_status":200,"reason":"ok"}
  ```

- **退出码 0**，stderr 为空；
- **落库变化**：`demo/checkflow.sqlite` 被创建并建好 `checks` 表，表中
  **恰好 1 行**：`id=1`，`url` 为中文原始 URL，`status=success`、
  `http_status=200`、`reason=ok`，`checked_at`、`elapsed_ms` 与 stdout
  逐字段一致（同一 dict 序列化而来，`healthcheck.py:486-508`）。

### 7.3 示例二：停服且端口无人监听时检查同一 URL（id=2，connection_error）

在终端 A 按 Ctrl-C 停止服务，并确认 8765 **无人监听**（例如
`ss -ltn | grep 8765` 无输出；没有其他进程占用该端口）。然后在终端 B 对
**同一数据库、同一 URL** 再跑一次完整调用：

```sh
python3 healthcheck.py --db demo/checkflow.sqlite check \
    --url 'http://127.0.0.1:8765/?q=健康%23' --timeout 1
```

确定结论：

- **发送的请求目标**：没有任何 HTTP 请求到达服务器——TCP 连接在本机即被
  拒绝（端口无人监听），不会发生重定向或重试；URL 编码规则不变（若建立
  了连接，目标仍是 `/?q=%E5%81%A5%E5%BA%B7%23`）；
- **stdout**：恰为一行 JSON，字段集合与键顺序同上，失败三字段固定：

  ```json
  {"id":2,"url":"http://127.0.0.1:8765/?q=健康%23","checked_at":"<探测结束后的UTC时刻>","elapsed_ms":<非负整数>,"status":"failure","http_status":null,"reason":"connection_error"}
  ```

- **退出码 1**（这是**已记录的探测失败**，不是参数错误），stderr 为空；
  拒连通常立即发生，一般远早于 1 秒超时，故归 `connection_error` 而非
  `timeout`；
- **落库变化**：`id=1` 的既有行**保持不变**，`checks` 表新增 **`id=2`**
  一行：`status=failure`、`reason=connection_error`、
  `http_status=NULL`，`url` 仍为中文原始 URL；全表共 **2 行**。

> 若端口上是「接受连接但不发响应」的服务，结局会变成 `timeout`
> （退出码同样为 1，但 `reason="timeout"`）；本示例要求的是**无人监听**，
> 对应 `connection_error`，复现时请勿混用。

### 7.4 `checked_at` 与 `elapsed_ms` 的判定口径（不核对精确值）

- `checked_at`：必须能被 `datetime.fromisoformat` 解析为**带时区的 UTC**
  时刻（`utcoffset() == 0`），形如
  `YYYY-MM-DDTHH:MM:SS.ffffff+00:00`；两条记录分别在各自探测结束后生成，
  故 id=2 的时刻不早于 id=1。**不要求等于任何固定字符串。**
- `elapsed_ms`：JSON 中是整数（且不是布尔值）、`>= 0`；落库列为
  `INTEGER NOT NULL CHECK (elapsed_ms >= 0)`。本机回环的两次探测通常都在
  个位数毫秒量级，拒连亦然，但**具体数值每次运行都可能不同，不作为验收
  定值**。

### 7.5 落库与记录的复核命令

两次检查后，可直接读库核对两行内容与表结构（按 id 升序）：

```sh
python3 - <<'EOF'
import sqlite3

conn = sqlite3.connect("demo/checkflow.sqlite")
print("columns:", [r[1] for r in conn.execute("PRAGMA table_info(checks)")])
for row in conn.execute(
    "SELECT id, url, checked_at, elapsed_ms, status, http_status, reason "
    "FROM checks ORDER BY id"
):
    print(row)
conn.close()
EOF
```

预期（依据当前源码推导；`<UTC 时刻>`、`<非负整数>` 为每次不同的占位值）：

```
columns: ['id', 'url', 'checked_at', 'elapsed_ms', 'status', 'http_status', 'reason']
(1, 'http://127.0.0.1:8765/?q=健康%23', '<UTC 时刻>', <非负整数>, 'success', 200, 'ok')
(2, 'http://127.0.0.1:8765/?q=健康%23', '<UTC 时刻>', <非负整数>, 'failure', None, 'connection_error')
```

也可用公开的只读入口复核（不发请求、不改动数据，行为见
`RECENT_QUERY_FLOW.md`）：

```sh
python3 healthcheck.py --db demo/checkflow.sqlite recent --limit 5
# 预期为按 id 倒序的两个记录对象：id=2（failure/connection_error/null）在前，
# id=1（success/ok/200）在后；两条 url 均为中文原始 URL。
```

---

## 8. 保持不变的既有约定

- **公开入口不变**：仍是 `main` → `command_check`
  （`healthcheck.py:787-790, 474-516`），`check` 只接受 `--db`、`--url`
  与可选 `--timeout`；它不接受 `recent` 的任何筛选参数。
- **数据库结构不变**：七字段 `checks` 表（`CREATE_TABLE_SQL`，
  `healthcheck.py:82-93`）、插入模板 `INSERT_SQL`
  （`healthcheck.py:95-98`）与必填字段集合 `REQUIRED_COLUMNS`
  （`healthcheck.py:102-104`）均无改动；缺字段只拒绝、不修复。
- **既有查询行为不变**：`recent` 仍严格只读、按 id 倒序、按原始 url
  精确匹配，本文不改变其任何路径（见 `RECENT_QUERY_FLOW.md`）。
- **常量语义不变**：主机白名单 `ALLOWED_HOST`、默认超时
  `DEFAULT_TIMEOUT`、两个状态与四个 reason 常量
  （`healthcheck.py:29-39`），以及退出码约定（成功 0 / 已记录的探测失败
  1 / 参数或数据库错误与写入失败 2）维持原样。

## 9. 结论到源码位置的对照

| 结论 | 函数 / 常量 | 位置 |
|---|---|---|
| check 处理总流程（校验 URL → 备库 → 探测 → 提交 → 输出） | `command_check` | `474-516` |
| 入口与参数分发 | `main` / check 子解析器 | `787-790` / `708-716` |
| 本机 URL 规则（http、127.0.0.1、显式端口、无 userinfo、无原始 `#`）与请求目标拼装 | `validate_target_url` | `339-389`，调用点 `475` |
| 主机白名单 / 默认超时 1.0 秒 | `ALLOWED_HOST` / `DEFAULT_TIMEOUT` | `29` / `30` |
| 超时必须为有限正数（argparse 阶段拒绝，退出码 2） | `positive_timeout` | `141-148` |
| 错误统一出口（退出码 2、stderr、stdout 空） | `die` | `135-138` |
| 探测前建库建表；父目录缺失/打不开/初始化失败请求前拒绝 | `open_database` | `392-408`，调用点 `478` |
| 既有表缺字段请求前拒绝；不补列、不重建表 | `ensure_checks_columns` / `REQUIRED_COLUMNS` | `411-423` / `102-104` |
| 表结构与插入模板（七字段、elapsed_ms 非负约束） | `CREATE_TABLE_SQL` / `INSERT_SQL` | `82-93` / `95-98` |
| 只发一次 GET、不跟随重定向；2xx/非2xx/超时/连接错误四分支 | `probe_once` | `443-467`，调用点 `482-484` |
| 非 ASCII 按 UTF-8 大写百分号编码上线；既有编码与原始 URL 保留 | `encode_request_target` | `426-440` |
| 状态与原因取值 | `STATUS_SUCCESS/FAILURE`、`REASON_OK/HTTP_STATUS/CONNECTION_ERROR/TIMEOUT` | `33-39` |
| `elapsed_ms` 计时口径（起于 request 前、止于响应头/异常，max(0,round)，不含 body 与全命令） | `probe_once` 计时段 | `449-465` |
| `checked_at` 在探测结束后生成（UTC ISO 8601） | `utc_now_iso` | `470-471`，调用点 `485` |
| 提交成功后才输出；成功 0 / 探测失败 1；写入失败 2 且 stdout 空 | INSERT/`commit`/输出与返回值 | `495-516` |
| JSON 七字段与键顺序、紧凑分隔、中文原样 | `record` dict + `json.dumps` | `486-515` |
