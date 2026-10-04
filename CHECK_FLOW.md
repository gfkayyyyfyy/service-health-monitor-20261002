# `check` 检查流程说明（参数校验 → 数据库准备 → 单次探测 → 落库 → JSON）

本说明描述 `healthcheck.py` **当前已实现**的 `check` 子命令完整路径：从命令行
参数进入，沿「参数校验 → 数据库准备 → 请求目标编码与单次 GET 探测 → 记录提交
→ 单行 JSON 输出」讲清每一步，并标注结论对应的函数或常量（行号以当前
`healthcheck.py` 为准），用于核对一次检查的结果字段与 `elapsed_ms` 的耗时含义。
本文不新增、不改变任何行为：公开入口、数据库结构（`CREATE_TABLE_SQL`）与
`recent` 的既有查询行为均保持不变；历史查询侧的完整流程见
`RECENT_QUERY_FLOW.md`。

- 入口函数：`main`（`healthcheck.py:787-790`）→ `command_check`
  （`healthcheck.py:474-516`）。
- 固定常量：`ALLOWED_HOST = "127.0.0.1"`（`healthcheck.py:29`）、
  `DEFAULT_TIMEOUT = 1.0`（`healthcheck.py:30`）；
  `STATUS_SUCCESS` / `STATUS_FAILURE`（`healthcheck.py:38-39`）与
  `REASON_OK` / `REASON_HTTP_STATUS` / `REASON_CONNECTION_ERROR` /
  `REASON_TIMEOUT`（`healthcheck.py:33-36`）。

> 文中所有命令输出块均为**依据当前源码逐条推导的预期结果**，不是「本文档生成时
> 已运行」的实测录屏。第 6 节给出两个可在本机自行复现的完整示例（共用一个全新
> 数据库），其中 `checked_at` 与 `elapsed_ms` 只给判定口径、不伪造精确值；
> 分类、退出码、请求目标、字段集合与 id 顺序则是由实现固定的结论。同类行为的
> 自动化对照见 `test_check_http_status.py`、`test_check_timeout.py`、
> `test_check_connection_error.py`、`test_check_unicode_url.py`、
> `test_check_malformed_url.py`、`test_check_missing_columns.py` 与
> `test_check_timeout_validation.py`。

---

## 1. 公开参数

命令形式（参数定义见 `build_parser`，`--db` 在 `healthcheck.py:705`，
`check` 子解析器在 `healthcheck.py:708-716`）：

```sh
python healthcheck.py --db <数据库路径> check \
    --url <本机HTTP目标URL> [--timeout <有限正数秒>]
```

| 参数 | 是否必填 | 默认 | 约束与语义 | 代码位置 |
|---|---|---|---|---|
| `--db` | 必填（全局） | 无 | SQLite 数据库文件路径；文件不存在时由 `check` 创建，**父目录必须已存在** | `healthcheck.py:705` |
| `--url` | 必填 | 无 | 仅接受 `http://127.0.0.1:<显式端口>[/path][?query]`，规则见第 1.1 节 | `healthcheck.py:709`，校验 `339-389` |
| `--timeout` | 可选 | `1.0`（`DEFAULT_TIMEOUT`，`healthcheck.py:30`） | 有限正数秒；非数字、`0`、负数、`nan`/`inf` 均被拒绝 | `positive_timeout`，`healthcheck.py:141-148`；参数定义 `710-715` |

`check` **不接受** `recent` 的 `--status`、`--reason`、`--since`、`--until`、
`--limit`、`--summary` 等任何参数（argparse 以退出码 `2` 报
`unrecognized arguments`）。

### 1.1 `--url` 的本机规则

由 `validate_target_url`（`healthcheck.py:339-389`）校验，返回
`(port, target)`，其中 `target` 为「路径（空路径按 `/`）+ 可选 `;params` +
可选 `?query`」：

- 必须是非空字符串，且不含任何空白或控制字符（`healthcheck.py:341-345`）；
- 结构必须能被 `urlparse` 解析、主机地址必须可解析（`:347-359`）；
- 协议必须恰好是 **`http`**（`:361-362`）；
- 主机必须恰好是 **`127.0.0.1`**（`ALLOWED_HOST`，`:363-364`），
  不接受 `localhost`、其他环回写法或任何非本机地址；
- 必须**显式指定端口**，且端口在 `1-65535`（`:365-373`）；
- 不接受用户信息（userinfo，`:374-375`）；
- **原始字符串中只要出现 `#` 即按片段（fragment）拒绝**（`:376-381`）；
  注意 `%23` 是普通百分号编码内容、不是原始 `#`，仍可合法出现在路径或查询中。

校验只用于判定输入是否合法；探测、落库与输出使用的都是命令行上的**原始
URL 字符串**，不做任何规范化。

### 1.2 `--timeout` 的有限正数规则

由 `positive_timeout`（`healthcheck.py:141-148`）在 argparse 解析期转换：

- 必须能转为 `float`，否则报「超时值必须是数字」；
- 必须 `math.isfinite` 且**严格大于 0**，否则报「超时必须是有限正数秒」；
  `0`、负数、`nan`、`inf`、`-inf` 都不合法。

该校验发生在 `command_check` 运行之前：非法超时由 argparse 直接拒绝，
**不会校验 URL、不会打开数据库、不会发请求**。

---

## 2. 端到端流程

`main` 解析参数后调用 `args.handler`，对 `check` 即 `command_check`
（`healthcheck.py:474-516`）。步骤严格按以下顺序发生：

1. **参数解析与校验**（argparse 期 + `healthcheck.py:475`）。
   `--timeout` 的类型转换在 argparse 期完成；进入处理函数后首先调用
   `validate_target_url(args.url)`。任何非法输入（缺必填参数、无法识别的参数、
   非法超时、非法 URL）一律以**退出码 `2`** 结束：**stdout 为空**，原因写入
   **stderr**，**不发请求、不新增检查记录**（此时数据库尚未打开）。
   argparse 自身错误与其后 `die`（`healthcheck.py:135-138`）的出口形态一致：
   退出码 `2`、只有 stderr、无 Python 回溯（`die` 的固定前缀为
   `healthcheck: error:`）。

2. **探测前准备数据库**（`open_database`，`healthcheck.py:392-408`，
   调用点 `:478`）。这一步**先于一切网络操作**，任一情形都经 `die` 以退出码
   `2` 拒绝，**不发请求、不新增检查记录**：
   - **数据库父目录不存在**：对 `db_path` 取绝对路径的父目录，
     `os.path.isdir(parent)` 为假即报「数据库父目录不存在: …」（`:398-400`），
     不创建目录；
   - **无法打开或初始化**：`sqlite3.connect`、建表执行或提交抛
     `sqlite3.Error`（如权限不足、路径是目录、磁盘故障等）时报
     「无法打开或初始化数据库 …」（`:401-407`）；
   - **已有 `checks` 表缺字段**：建表语句是
     `CREATE TABLE IF NOT EXISTS`（`CREATE_TABLE_SQL`，
     `healthcheck.py:82-93`），表已存在时为 no-op；随后
     `ensure_checks_columns`（`healthcheck.py:411-423`）用
     `PRAGMA table_info(checks)` 核对 `REQUIRED_COLUMNS`
     （`healthcheck.py:102-104`：`id, url, checked_at, elapsed_ms, status,
     http_status, reason`）七个字段，缺任意一个即报「checks 表缺少字段: …」。
     **缺字段时不补列（不执行 `ALTER TABLE`）、不重建表、不搬数据**，
     原有表结构与内容保持不变；字段名按 SQLite 语义大小写不敏感比较，
     列顺序不同或有额外列均不构成问题。

   文件不存在而父目录存在时，本步会创建数据库文件与 `checks` 表（即第 6 节
   示例一中「文件与表的建立」发生在探测之前）；库与表均已存在且结构完好时，
   本步不改动任何既有内容。

3. **编码请求目标并发送唯一一次 GET**（调用点 `healthcheck.py:482-484`；
   `encode_request_target` 在 `426-440`，`probe_once` 在 `443-467`）。
   编码与探测规则见第 3、4 节：合法输入**只发一次 GET、不重试、不跟随
   重定向**；非 ASCII 的路径和查询内容按 UTF-8 百分号编码后发送，已有百分号
   编码原样保留；**落库与输出仍使用原始 URL**。

4. **探测结束后生成 `checked_at`**（`healthcheck.py:485`）。
   `checked_at = utc_now_iso()`（`healthcheck.py:470-471`，
   `datetime.now(timezone.utc).isoformat()`）在 `probe_once` 返回**之后**
   取时刻，因此它标记的是**探测结束时刻**，而非请求开始时刻；形态为带
   `+00:00` 偏移的 UTC ISO 8601（如 `2026-10-05T08:30:00.123456+00:00`）。

5. **组装并提交记录**（`healthcheck.py:486-510`）。记录对象按固定顺序包含
   七个字段：`id`（提交前为 `null`）、`url`（**原始输入**）、`checked_at`、
   `elapsed_ms`、`status`、`http_status`、`reason`。通过 `INSERT_SQL`
   （`healthcheck.py:95-98`）插入并 `commit`，成功后以 `cur.lastrowid`
   回填 `id`（`:496-508`；新库首条为 1，`id INTEGER PRIMARY KEY
   AUTOINCREMENT`，见 `CREATE_TABLE_SQL`）。插入或提交抛 `sqlite3.Error` 时
   经 `die` 报「写入检查记录失败: …」：**退出码 `2`、stdout 为空**
   （探测本身此时已经发生，但没有任何输出，也没有成功提交的记录）。

6. **提交成功后才输出 JSON**（`healthcheck.py:515-516`）。
   `print(json.dumps(record, separators=(",", ":"), ensure_ascii=False))`
   输出**恰好一行**紧凑 JSON（末尾一个换行），非 ASCII 字符按原文直接输出
   （`ensure_ascii=False`，中文不转义）。随后返回退出码：
   **探测成功 `0`，已落库的探测失败 `1`**（`:516`）。stderr 在这两种情况下
   均为空。

---

## 3. 请求目标编码：非 ASCII 按 UTF-8 百分号编码，原文另行保留

`encode_request_target`（`healthcheck.py:426-440`）只作用于**发送时的请求
目标**，逐字符处理：

- 码位大于 127 的字符：按其 **UTF-8 字节**逐字节转为**大写十六进制**百分号
  编码（`%XX`，字母大写）。例如「健」「康」分别为
  `%E5%81%A5`、`%E5%BA%B7`；
- 其余 ASCII 字符（含路径分隔符 `/`、查询的 `?`、`=`、`&`、`+`、
  以及**已有的百分号编码**）一律原样保留：`%23` 仍是 `%23`，
  **不会被二次编码成 `%2523`**，原有大小写也不变；
- 参数顺序与空白（合法输入不含空白）等均不重排。

因此**落库的 `url`、stdout JSON 里的 `url` 与命令行输入逐字相同**
（`record["url"] = args.url`，`healthcheck.py:488`），编码结果只出现在网络
请求行中；两个不同的原始输入即使编码后的请求目标相同，也仍是两条独立记录
（见 `test_check_unicode_url.py` 中中文原文与预编码地址各落一条的对照）。

---

## 4. 单次探测与结果分类（`probe_once`）

`probe_once`（`healthcheck.py:443-467`）对 `http.client.HTTPConnection`
（主机固定 `ALLOWED_HOST`，超时取 `--timeout`）执行**且仅执行一次**
`conn.request("GET", target)`（`:448-452`），随后 `conn.getresponse()`
读取状态行与响应头（`:453-454`），`finally` 中关闭连接（`:466-467`），
**没有任何重试**。标准库 `http.client` 本身不自动跟随重定向，因此 3xx 响应
按实际状态码归入 `http_status` 失败，不会补发第二次 GET。

| 探测结局 | 判定位置 | `status` | `reason` | `http_status` | 退出码 |
|---|---|---|---|---|---|
| 收到 **2xx** 响应头 | `healthcheck.py:456-457` | `success` | `ok` | 收到的状态码（200–299） | `0` |
| 收到**其他状态码**响应头（3xx/4xx/5xx 等，含重定向） | `healthcheck.py:458` | `failure` | `http_status` | 收到的状态码 | `1` |
| 超时（`TimeoutError`，含连接/发送/等待响应头超时） | `healthcheck.py:459-461` | `failure` | `timeout` | `null` | `1` |
| 其他连接错误（`OSError`：连接被拒/重置等；`http.client.HTTPException`：响应畸形、对端直接断开等） | `healthcheck.py:462-465` | `failure` | `connection_error` | `null` | `1` |

后两类失败在响应头到达**之前**就已结束，没有状态码可言，故 `http_status`
保存为 Python `None`，JSON 中即 `null`；四类 `reason` 的取值与表约束
（`CREATE_TABLE_SQL` 中的 `CHECK`，`healthcheck.py:90-91`）及常量
`REASON_*`（`healthcheck.py:33-36`）一一对应。「端口无人监听」时本机环回
立即返回连接拒绝，抛 `ConnectionRefusedError`（`OSError` 子类），落入最后
一类，即 `connection_error` 而**不是** `timeout`（无需等到超时）。

---

## 5. `elapsed_ms` 与 `checked_at` 的口径（核对耗时含义用）

**`elapsed_ms` 是「探测段」耗时，不是整条命令的总耗时。** 由
`probe_once` 内部计时（`healthcheck.py:449-465`）：

- 起点 `start = time.monotonic()`（`:449`）在 `HTTPConnection` 对象构造
  **之后**、`conn.request(...)` **之前**，因此包含 TCP 连接建立、请求发送与
  等待响应的时间，但**不包含**进程启动、参数解析、数据库打开/建表
  （`open_database` 在探测之前）、记录插入与提交（在探测之后）、JSON 输出；
- 成功或收到非 2xx 响应时，计时在 `getresponse()` 返回并读到
  `resp.status` 后截止（`:453-455`）——`getresponse()` 在**状态行与响应头
  解析完成时即返回**，代码**不调用 `resp.read()`**，因此
  **不包含完整响应体读取**的时间；响应体大小与传输快慢不影响该值；
- 超时或其他连接错误时，计时在捕获到异常处截止（`:459-465`）；
- 取值为 `max(0, round((time.monotonic() - start) * 1000))`（`:455, 460,
  464`）：单调时钟差值换算毫秒、四舍五入取整，并以 `max(0, …)` 钳为非负；
  表约束另有 `CHECK (elapsed_ms >= 0)`（`healthcheck.py:87`）。因此 JSON 中
  它恒为**非负整数**（无引号的 JSON 数字，`0` 合法），具体数值随机器与网络
  变化，文档与用例都不应伪造或断言精确毫秒。

**`checked_at` 在探测结束后才生成**（`healthcheck.py:485`，
见第 2 节第 4 步），与 `elapsed_ms` 一起描述同一次探测：前者是结束的 UTC
时刻，后者是探测段经过的毫秒数；它同样不取开始前的时刻，也不覆盖数据库写入
所花的时间。

---

## 6. 两个可复核的本地示例（共用一个全新数据库）

两个示例共用同一个**全新**数据库 `/tmp/hc-check-flow/monitor.sqlite`，
其父目录 `/tmp/hc-check-flow` 事先建好，数据库文件事先不存在。两次检查使用
**同一原始 URL**：

```
http://127.0.0.1:8765/?q=健康%23
```

它通过第 1.1 节全部规则（`http`、主机 `127.0.0.1`、显式端口 8765、
无原始 `#`——`%23` 是查询值里的普通百分号编码）。无论服务是否在，
**发送时的请求目标**都由第 3 节规则固定为：

```
/?q=%E5%81%A5%E5%BA%B7%23
```

即请求行为 `GET /?q=%E5%81%A5%E5%BA%B7%23 HTTP/1.1`（「健康」按 UTF-8
变为 `%E5%81%A5%E5%BA%B7%23`，原有的 `%23` 原样保留、不重复编码）；
落库与输出的 `url` 始终是中文原文
`http://127.0.0.1:8765/?q=健康%23`。

### 6.0 准备：新建父目录并确认数据库尚不存在

```sh
mkdir -p /tmp/hc-check-flow
rm -f /tmp/hc-check-flow/monitor.sqlite
ls -la /tmp/hc-check-flow   # 预期：目录存在且为空（无 monitor.sqlite）
```

另开一个终端（下称「服务终端」）启动一个对任意 GET **立即返回 200** 并把
实际请求目标打印出来的本机服务（仅标准库，绑定 `127.0.0.1:8765`）：

```sh
python3 - <<'PY'
import http.server

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        print("收到请求目标:", self.path, flush=True)  # 核对线上字节
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

http.server.ThreadingHTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
PY
```

> 若 8765 已被占用，先停用占用方；下面的检查命令与 URL 均以 8765 为准。

### 6.1 示例一：服务存活、立即返回 200 → id=1 成功记录

在检查终端运行（`--timeout 2` 可省略，默认 `1.0` 秒）：

```sh
python3 healthcheck.py --db /tmp/hc-check-flow/monitor.sqlite check \
    --url 'http://127.0.0.1:8765/?q=健康%23' --timeout 2
```

预期（源码推导；退出码 `0`、stderr 为空、stdout 恰好一行）：

```json
{"id":1,"url":"http://127.0.0.1:8765/?q=健康%23","checked_at":"<探测结束时刻的UTC ISO-8601>","elapsed_ms":<非负整数毫秒>,"status":"success","http_status":200,"reason":"ok"}
```

固定可核对的结论：

- **发送的请求目标**：服务终端应恰好打印一次
  `收到请求目标: /?q=%E5%81%A5%E5%BA%B7%23`——只有一次 GET，中文为大写
  UTF-8 百分号编码，原有 `%23` 原样保留；
- **JSON 固定字段与顺序**：恰好为 `id, url, checked_at, elapsed_ms,
  status, http_status, reason` 七个；`id` 为 `1`，`url` 与输入逐字相同
  （中文不转义），`status` 为 `"success"`、`http_status` 为 `200`、
  `reason` 为 `"ok"`；
- **退出码 `0`**，stderr 为空；
- **落库变化**：因为数据库文件此前不存在而父目录存在，`open_database`
  在**探测之前**创建了 `monitor.sqlite` 与 `checks` 表（`CREATE TABLE IF
  NOT EXISTS`）；探测返回后 `INSERT_SQL` 提交**恰好一行**，即 id=1 的成功
  记录，其各字段与 stdout 逐字段一致。
- 仅给判定口径、不写死的两项：
  - `checked_at`：必须能按带时区的 ISO 8601 解析，UTC 偏移为 `+00:00`，
    且时刻不早于本次探测开始；
  - `elapsed_ms`：JSON 数字类型的**非负整数**（`0` 合法），具体值不核对。

### 6.2 示例二：停止服务且端口无人监听 → id=2 连接失败记录

在服务终端按 `Ctrl-C` 停止服务。可用以下纯标准库命令确认该端口已无人监听
（输出 `IDLE` 即可）：

```sh
python3 - <<'PY'
import socket
s = socket.socket()
rc = s.connect_ex(("127.0.0.1", 8765))
print("LISTENING" if rc == 0 else "IDLE", rc)
s.close()
PY
```

然后对**同一 URL**、**同一数据库**再次运行同一条检查命令：

```sh
python3 healthcheck.py --db /tmp/hc-check-flow/monitor.sqlite check \
    --url 'http://127.0.0.1:8765/?q=健康%23' --timeout 2
```

预期（源码推导；退出码 `1`、stderr 为空、stdout 恰好一行）：

```json
{"id":2,"url":"http://127.0.0.1:8765/?q=健康%23","checked_at":"<探测结束时刻的UTC ISO-8601>","elapsed_ms":<非负整数毫秒>,"status":"failure","http_status":null,"reason":"connection_error"}
```

固定可核对的结论：

- **发送的请求目标**：客户端仍按 `GET /?q=%E5%81%A5%E5%BA%B7%23
  HTTP/1.1` 准备请求行，但对 `127.0.0.1:8765` 的 TCP 连接被立即拒绝
  （`ConnectionRefusedError`，`OSError` 子类），**HTTP 请求字节不会到达任何
  服务端**；失败发生在响应头之前，故 `http_status` 为 `null`，分类为
  `connection_error` 而非 `timeout`（不必等到 2 秒超时）；
- **JSON 固定字段与顺序**：同样七个字段；`id` 为 `2`，`url` 仍是中文原文，
  `status` 为 `"failure"`、`http_status` 为 `null`、
  `reason` 为 `"connection_error"`；
- **退出码 `1`**（已落库的探测失败），stderr 为空；
- **落库变化**：数据库与 `checks` 表已在示例一建立，本步不重建、不改结构；
  在 id=1 之外**新增恰好一行** id=2，id=1 的成功记录原样保留，全表共 2 行。
  `checked_at` 与 `elapsed_ms` 的判定口径同示例一（UTC 可解析、非负整数
  毫秒），具体值不伪造。

### 6.3 统一核对：两次落库内容

两次检查后，可直接按 id 升序读出关键列（也可用
`python healthcheck.py --db /tmp/hc-check-flow/monitor.sqlite recent --limit 5`
得到按 id 倒序的等价两行）：

```sh
python3 - <<'PY'
import sqlite3
conn = sqlite3.connect("/tmp/hc-check-flow/monitor.sqlite")
for row in conn.execute(
    "SELECT id, url, status, http_status, reason FROM checks ORDER BY id"
):
    print(row)
conn.close()
PY
```

预期（源码推导；两条 `url` 均为中文原文）：

```text
(1, 'http://127.0.0.1:8765/?q=健康%23', 'success', 200, 'ok')
(2, 'http://127.0.0.1:8765/?q=健康%23', 'failure', None, 'connection_error')
```

复现完毕后清理示例资源：

```sh
rm -rf /tmp/hc-check-flow   # 可选：删除示例数据库
```

---

## 7. 退出码与错误通道汇总

| 情形 | 退出码 | stdout | stderr | 是否发请求 | 是否新增记录 |
|---|---|---|---|---|---|
| 收到 2xx（`success`/`ok`） | `0` | 一行成功 JSON | 空 | 恰好一次 GET | 新增 1 条 |
| 收到非 2xx 状态码（`failure`/`http_status`） | `1` | 一行失败 JSON（含状态码） | 空 | 恰好一次 GET | 新增 1 条 |
| 超时（`failure`/`timeout`） | `1` | 一行失败 JSON（`http_status:null`） | 空 | 一次未完成的 GET | 新增 1 条 |
| 其他连接错误（`failure`/`connection_error`） | `1` | 一行失败 JSON（`http_status:null`） | 空 | 一次未完成/无响应的 GET | 新增 1 条 |
| 缺必填参数、无法识别的参数 | `2` | 空 | argparse 用法与原因 | 否 | 否 |
| `--timeout` 非有限正数 | `2` | 空 | 「超时值必须是数字」/「超时必须是有限正数秒」 | 否 | 否 |
| `--url` 不符合本机规则 | `2` | 空 | `healthcheck: error:` 前缀的具体原因 | 否 | 否 |
| 数据库父目录不存在 | `2` | 空 | 「数据库父目录不存在: …」 | 否 | 否 |
| 数据库无法打开或初始化 | `2` | 空 | 「无法打开或初始化数据库 …」 | 否 | 否 |
| 已有 `checks` 表缺字段 | `2` | 空 | 「checks 表缺少字段: …」 | 否（不补列、不重建表） | 否 |
| 记录插入或提交失败 | `2` | 空 | 「写入检查记录失败: …」 | 是（探测已发生） | 无成功提交的记录 |

所有退出码 `2` 的通道（argparse 与 `die`，`healthcheck.py:135-138`）
都满足**stdout 完全为空、原因只写 stderr、无 Python 回溯**。

---

## 8. 保持不变的既有约定

- **公开入口不变**：仍是 `python healthcheck.py --db … check --url …
  [--timeout …]`（`build_parser`，`healthcheck.py:700-716`），
  `check` 不新增任何筛选参数。
- **数据库结构不变**：仍只有 `CREATE_TABLE_SQL`
  （`healthcheck.py:82-93`）定义的 `checks` 表与七字段、各 `CHECK` 约束；
  插入仍只走 `INSERT_SQL`（`healthcheck.py:95-98`）。缺字段只拒绝、
  不补列、不重建。
- **既有查询行为不变**：本文不涉及 `recent`；其只读查询、表名大小写不敏感
  识别、筛选与摘要行为以 `RECENT_QUERY_FLOW.md` 为准。
- **分类与退出码约定不变**：2xx→`success`/`ok`/0；非 2xx→
  `failure`/`http_status`/1；超时→`failure`/`timeout`/1；其他连接错误→
  `failure`/`connection_error`/1；参数与数据库错误→2（stdout 空）。
- **时间与耗时口径不变**：`checked_at` 为探测结束后的 UTC ISO 8601
  （`utc_now_iso`），`elapsed_ms` 为截止到响应头到达或异常发生的非负整数
  毫秒，二者都在第 5 节口径内被 recent 的时间筛选与摘要直接读取，不做转换。

---

## 9. 结论到源码位置的对照

| 结论 | 函数 / 常量 | 位置 |
|---|---|---|
| check 处理总流程（校验→开库→探测→落库→输出） | `command_check` | `474-516` |
| 入口分发 | `main` / `build_parser`（`check` 子解析器） | `787-790` / `708-716` |
| 仅本机 `http://127.0.0.1:<端口>`、拒绝 userinfo/片段等 | `validate_target_url` / `ALLOWED_HOST` | `339-389` / `29`，调用点 `475` |
| 超时必须为有限正数、默认 1.0 秒 | `positive_timeout` / `DEFAULT_TIMEOUT` | `141-148` / `30` |
| 错误统一出口（退出码 2、stderr、stdout 空） | `die` | `135-138` |
| 探测前打开/建库；父目录缺失、打开失败即拒绝 | `open_database` | `392-408`，调用点 `478` |
| 建表语句（`IF NOT EXISTS`、七字段与约束） | `CREATE_TABLE_SQL` | `82-93` |
| 既有表缺字段在请求前拒绝；不补列、不重建 | `ensure_checks_columns` / `REQUIRED_COLUMNS` | `411-423` / `102-104` |
| 非 ASCII 按 UTF-8 大写百分号编码、已有编码保留；只用于线上 | `encode_request_target` | `426-440`，调用点 `482-484` |
| 只发一次 GET、不跟随重定向；结果四分类与 `elapsed_ms` 计时 | `probe_once` | `443-467` |
| 2xx→success/ok；其他状态码→failure/http_status | `probe_once` 2xx 分支 | `456-458` |
| 超时→failure/timeout、`http_status=null` | `probe_once` `TimeoutError` 分支 | `459-461` |
| 其他连接错误→failure/connection_error、`http_status=null` | `probe_once` `OSError`/`HTTPException` 分支 | `462-465` |
| 状态/原因常量 | `STATUS_SUCCESS`/`STATUS_FAILURE`、`REASON_*` | `33-39` |
| `checked_at` 在探测结束后按 UTC ISO 8601 生成 | `utc_now_iso` | `470-471`，调用点 `485` |
| 记录插入、提交、回填 id；写入失败退出码 2 且无输出 | `INSERT_SQL` / 插入与 `die` 分支 | `95-98` / `496-510` |
| 提交成功后才输出紧凑单行 JSON（中文不转义）；成功 0/失败 1 | JSON 输出与返回值 | `515-516` |
