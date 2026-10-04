# 本地服务健康监测台

建设用于自有开发服务的轻量健康监测产品，逐步覆盖探测目标配置、HTTP 与 TCP 检查、状态历史、响应时间摘要、告警规则、本地事件列表和报表。

计划采用：Python 3 标准库 / urllib / socket / sqlite3 / argparse。

## 当前功能（最小可用）

`healthcheck.py` 仅使用 Python 3 标准库，提供：

- `check`：对**登记的单个 URL** 发送**一次** `GET`（不跟随重定向），将结果以一行 JSON 输出到标准输出并持久化到 SQLite；
- `recent`：**只读**查询历史，按 `id` 倒序输出 JSON 数组；不新增表或记录、不创建数据库文件或目录，也不发起任何网络请求（数据库可读但不可写时同样可查询）。历史表名按大小写不敏感识别：`CHECKS`、`Checks` 等与 `checks` 是同一历史表，查询结果完全相同。可选 `--url` 仅返回指定目标的记录：按数据库保存的原始 `url` 字符串**精确匹配**，不合并路径或查询参数不同的地址，也不规范化 URL；未提供时返回全部目标的记录。筛选值沿用 `check` 的本机 URL 规则，非法地址以退出码 `2` 结束，且优先于数据库路径错误报告。可选 `--status` 仅返回指定状态的记录，只接受区分大小写的 `success` 或 `failure`（其他值、空字符串或缺少值均为参数错误，退出码 `2`，在读取数据库前拒绝）；省略时查询全部状态。可选 `--reason` 仅返回保存的 `reason` 与之精确相等的记录，只接受区分大小写的 `ok`、`http_status`、`connection_error`、`timeout`（其他值、大小写变体、前后空白、空字符串或缺少值均为参数错误，退出码 `2`，在 status、URL 均合法之后、读取数据库之前拒绝）；只按保存值匹配、不从状态码推断，省略时查询全部原因。三个筛选同用时各条件取交集；查询先按全部条件筛选，再按 `id` 倒序取 `--limit` 条（默认 5）。

目标 URL 限制：仅 `http` 协议、主机必须为 `127.0.0.1`、必须显式指定 `1-65535` 端口；允许路径与查询参数；不接受用户信息（userinfo）与片段（fragment）；其余地址一律拒绝。本轮不做服务自动发现，也不含 TCP、定时任务或告警。

记录字段：递增 `id`、原始 `url`、UTC 时间（ISO 8601）`checked_at`、非负整数毫秒耗时 `elapsed_ms`、`status`（`success`/`failure`）、`http_status`（收到响应时为状态码，否则 `null`）、`reason`（`ok` / `http_status` / `connection_error` / `timeout`）。

退出码：成功 `0`；已记录的探测失败 `1`；非法 URL、参数不合法、数据库无法打开或读写失败 `2`（标准输出为空，说明写入标准错误；无效输入不发请求、不新增记录；探测前数据库不可用也不发请求）。`recent` 在路径是目录、文件不是有效 SQLite 数据库、读取权限不足、或历史表（含 `CHECKS`/`Checks` 等大小写异写）缺少查询所需字段时同样以 `2` 结束（标准输出为空、标准错误说明原因），不尝试修复、重建或覆盖文件。

数据库文件不存在时 `check` 可创建（父目录必须已存在）；`recent` 对不存在的路径（即使父目录也不存在，且不会创建任何文件或目录）、空数据库、没有历史表（包括仅有其他表）的数据库以及历史表存在但无记录的情况均输出 `[]`，库中已有的表与数据保持不变。

参数：`--timeout` 默认 `1`，须为有限正数秒；`--limit` 默认 `5`，须为正整数。

## 本地使用示例

```sh
# 终端 A：启动一个可控的本机演示服务
python3 -m http.server 8765 --bind 127.0.0.1

# 终端 B：服务存活时检查（仅一次 GET，2xx 即成功）
python3 healthcheck.py --db monitor.sqlite check --url http://127.0.0.1:8765/
# {"id":1,"url":"http://127.0.0.1:8765/","checked_at":"2026-10-02T04:40:00.000000+00:00","elapsed_ms":3,"status":"success","http_status":200,"reason":"ok"}
# 退出码 0

# 终端 A：Ctrl-C 停止服务；终端 B 再查同一 URL（连接失败也会落库）
python3 healthcheck.py --db monitor.sqlite check --url http://127.0.0.1:8765/
# {"id":2,"url":"http://127.0.0.1:8765/","checked_at":"2026-10-02T04:40:05.000000+00:00","elapsed_ms":1,"status":"failure","http_status":null,"reason":"connection_error"}
# 退出码 1

# 查询历史：失败在前、成功在后
python3 healthcheck.py --db monitor.sqlite recent --limit 5
# [{"id":2,...,"status":"failure","http_status":null,"reason":"connection_error"},
#  {"id":1,...,"status":"success","http_status":200,"reason":"ok"}]

# 关闭后重新启动查询进程，仍能从同一数据库读到这两条记录
python3 healthcheck.py --db monitor.sqlite recent
```

路径与查询参数同样允许，例如：

```sh
python3 healthcheck.py --db monitor.sqlite check \
    --url 'http://127.0.0.1:8765/health?detail=1' --timeout 2
```
