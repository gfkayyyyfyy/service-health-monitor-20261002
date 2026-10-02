# 本地服务健康监测台

建设用于自有开发服务的轻量健康监测产品，逐步覆盖探测目标配置、HTTP 与 TCP 检查、状态历史、响应时间摘要、告警规则、本地事件列表和报表。

计划采用：Python 3 标准库 / urllib / socket / sqlite3 / argparse。

## 当前功能（最小可用）

`healthcheck.py` 仅使用 Python 3 标准库，提供：

- `check`：对**登记的单个 URL** 发送**一次** `GET`（不跟随重定向），将结果以一行 JSON 输出到标准输出并持久化到 SQLite；
- `recent`：只查询历史，按 `id` 倒序输出 JSON 数组。

目标 URL 限制：仅 `http` 协议、主机必须为 `127.0.0.1`、必须显式指定 `1-65535` 端口；允许路径与查询参数；不接受用户信息（userinfo）与片段（fragment）；其余地址一律拒绝。本轮不做服务自动发现，也不含 TCP、定时任务或告警。

记录字段：递增 `id`、原始 `url`、UTC 时间（ISO 8601）`checked_at`、非负整数毫秒耗时 `elapsed_ms`、`status`（`success`/`failure`）、`http_status`（收到响应时为状态码，否则 `null`）、`reason`（`ok` / `http_status` / `connection_error` / `timeout`）。

退出码：成功 `0`；已记录的探测失败 `1`；非法 URL、参数不合法、数据库无法打开或读写失败 `2`（标准输出为空，说明写入标准错误；无效输入不发请求、不新增记录；探测前数据库不可用也不发请求）。

数据库文件不存在时 `check` 可创建（父目录必须已存在）；空数据库或文件尚不存在时 `recent` 输出 `[]`。

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
