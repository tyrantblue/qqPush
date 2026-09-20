# qqpush 调用文档

面向调用方（监控、CI、脚本）的完整接口说明。部署与运维见 [README.md](README.md)。

- 默认地址：`http://<服务地址>:8088`
- 所有响应都是 JSON（UTF-8）
- 需要鉴权时，每个请求都要带 token（见下）

---

## 1. 鉴权

服务端设置 `QQPUSH_TOKEN` 后，请求必须携带以下任一种：

| 方式 | 示例 |
| --- | --- |
| 请求头（推荐） | `X-Push-Token: <token>` |
| Bearer | `Authorization: Bearer <token>` |
| 查询串（仅调试用） | `?token=<token>` |

未通过返回 `401`：

```json
{"ok": false, "error": "鉴权失败：token 不正确"}
```

> 例外：`GET /healthz` 永远不需要鉴权，方便探针使用。

---

## 2. 获取 group_openid（官方机器人通道）

官方接口发群消息用 **`group_openid`**，它在开放平台面板里看不到，只能通过事件获取。

### 2.1 三条获取途径

| 途径 | 命令 | 说明 |
| --- | --- | --- |
| 自动获取（推荐） | 起服务后 **在群里 @ 一下机器人** | 只剩一个待绑定群时服务自动写回 `data/groups.json`，并记入 `/openids` |
| 手动取号 | `uv run qqpush-discover` | 监听并打印 `group_openid`，Ctrl+C 退出 |
| HTTP 查询 | `GET /openids` | 查询服务当前已知的群名 → openid 映射 |

**前提**：机器人必须在目标群里；事件是**即时推送**的，**服务必须正在运行**（不会补发历史事件）。

### 2.2 `GET /openids`（别名 `GET /groups`、`GET /targets`）

查询当前可推送的目标与对应的 `group_openid`。

```bash
curl -H "X-Push-Token: <token>" http://127.0.0.1:8088/openids
```

```json
{
  "ok": true,
  "channel": "official",
  "target_hint": "群名（或直接填 group_openid）",
  "targets": ["运维告警"],
  "openids": [
    { "name": "运维告警", "target": "B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5" }
  ],
  "default_targets": ["运维告警"],
  "app_id": "1234567890",
  "token_expires_in": 7192
}
```

| 字段 | 含义 |
| --- | --- |
| `channel` | `official`（官方机器人）或 `onebot`（NapCat 等） |
| `targets` | 可用的群名列表 |
| `openids[].name` | 群名 |
| `openids[].target` | 该通道实际发送用的 ID：官方通道是 `group_openid`，OneBot 通道是群号 |
| `target_hint` | 传参提示 |
| `token_expires_in` | 官方 access_token 剩余有效秒数（仅 official） |

> 需要"人肉"首次取号时用 `qqpush-discover`；群多了以后（多个群同时待绑定），服务不会猜，
> 会在日志里打印 openid 让你手工写进 `data/groups.json` 的 `group_openids`。

---

## 3. 推送消息：`POST /push`

```bash
curl -X POST http://127.0.0.1:8088/push \
  -H "X-Push-Token: <token>" \
  -H "Content-Type: application/json" \
  -d '{"group_openid": "B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5", "message": "磁盘使用率 95%"}'
```

### 3.1 请求参数

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `group_openid` | string | 二选一 | 目标群 openid。也可用 `openid` / `groups` / `targets` / `to`（同义） |
| `groups` | string \| array | 二选一 | 目标：**群名**或 `group_openid`（或 OneBot 通道的群号） |
| `message` | string \| array | 二选一 | 消息内容，格式见第 4 节 |
| `messages` | array | 二选一 | 批量推送多条，元素格式同 `message` |
| `title` | string | 否 | 标题行，渲染为 `【标题】` |
| `prefix` | string | 否 | 固定前缀，缺省用服务端 `QQPUSH_DEFAULT_PREFIX` |
| `markdown` | bool | 否 | 是否把 markdown 降级为纯文本，缺省用服务端配置（默认 `true`） |
| `id` | string | 否 | 幂等 ID：`QQPUSH_DEDUP_TTL` 秒内重复提交只发一次 |
| `source` | string | 否 | 来源标记，仅用于日志与失败记录 |

不传任何目标时使用服务端配置的 `default_targets`。

### 3.2 响应

`202 Accepted`（已受理，发送在后台完成）：

```json
{
  "ok": true,
  "job_id": "bcc29d3fd332",
  "job_ids": ["bcc29d3fd332"],
  "targets": ["B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5"],
  "names": ["运维告警"],
  "group_openid": "B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5",
  "chunks": 1,
  "duplicates": 0,
  "rejected": null
}
```

| 字段 | 含义 |
| --- | --- |
| `job_id` / `job_ids` | 任务号；消息超长被分片时会有多个 |
| `targets` | 实际发送用的 ID（官方通道是 group_openid） |
| `names` | 上面的 ID 对应的群名（便于日志对账） |
| `group_openid` | 单一目标时直接给出 openid，方便首次调用后记录下来复用 |
| `chunks` | 分片数（`1` 表示没被切分） |
| `duplicates` | 因幂等被忽略的条数 |

`200 OK`：`id` 命中幂等去重，未重复发送：

```json
{"ok": true, "duplicate": true, "message": "重复消息，已忽略"}
```

### 3.3 状态码

| 状态码 | 含义 | 处理 |
| --- | --- | --- |
| `202` | 已受理 | 正常 |
| `200` | 重复消息（幂等命中） | 正常，无需重试 |
| `400` | 请求体不合法 | 修参数，别重试 |
| `401` | token 不正确 | 检查 token |
| `404` | 群未配置（`error` 里有「未知的群」，`known` 给出可用群名） | 检查 `groups.json` |
| `409` | 群已配置但**还没有 group_openid**（`pending_openid` 列出） | 让机器人进群并 @ 一次，然后重试 |
| `429` | 队列已满，服务繁忙 | 稍后重试 |
| `500` | 服务内部错误 | 重试并看服务端日志 |

> **投递结果（成功/失败原因）不在这个响应里**：`202` 只代表入队成功。
> 需要确认送达与否，用 `GET /status/<job_id>`（见第 5 节），或直接看 `data/failed.ndjson`。

---

## 4. 消息格式

官方机器人通道**只发文本**（`msg_type=0`），本服务会把各种输入统一转成文本。
OneBot 通道则支持完整消息段。

### 4.1 纯文本（最常用）

```json
{"group_openid": "OPENID", "message": "磁盘使用率 95%，请尽快处理"}
```

### 4.2 Markdown（默认自动降级为纯文本）

`markdown=true`（默认）时，常见语法会被降级成可读文本，避免 QQ 里出现一堆 `**`：

| 输入 | 输出 |
| --- | --- |
| `**粗体**` | `粗体` |
| `` `code` `` | `code` |
| ```` ```lang\ncode\n``` ```` | `code`（去掉围栏） |
| `[文字](https://x)` | `文字 (https://x)` |
| `![图](https://x/a.png)` | `[图片:图] https://x/a.png` |
| `# 标题` | `【标题】` |
| `- 项` | `· 项` |

```json
{
  "group_openid": "OPENID",
  "title": "CI",
  "message": "# 构建失败\n**仓库**：qqpush\n见 [日志](https://ci/1)"
}
```

发送结果：

```
【CI】
构建失败
仓库：qqpush
见 日志 (https://ci/1)
```

传 `"markdown": false` 可关闭降级，原样发送（需自担 QQ 渲染效果）。

### 4.3 消息段数组（OneBot 风格）

官方通道会把非文本段降级为文字占位；OneBot 通道会原样发送图片等富媒体。

```json
{
  "group_openid": "OPENID",
  "message": [
    { "type": "text",  "data": { "text": "图一 " } },
    { "type": "image", "data": { "file": "https://example.com/a.png" } }
  ]
}
```

支持的段类型（白名单）：`text`、`image`、`face`、`at`、`record`、`video`、`reply`、`json`、`music`。

| 段类型 | 官方通道渲染 |
| --- | --- |
| `text` | 原文本 |
| `image` | `[图片] <url>` |
| `at` | `@123456`（`all` → `全体成员`） |
| `record` / `video` / `face` | `[语音]` / `[视频]` / `[表情]` |

> 官方通道要发真正的图片需要先调官方文件上传接口拿 `file_info`，本服务暂未代理该流程。

### 4.4 批量推送

```json
{
  "group_openid": "OPENID",
  "messages": ["第一条", "第二条"]
}
```

### 4.5 长度与转义

| 项 | 行为 |
| --- | --- |
| 官方通道长度 | 超过 `QQBOT_MAX_CHARS`（默认 1000）会被截断并加 `…` |
| OneBot 长度 | 超过 `QQPUSH_MAX_CHARS`（默认 4500）自动按换行/句末切分成多条 |
| CQ 码转义 | 文本里的 `[`、`]`、`&`、`,` 会转义为 `&#91;` 等，防止被协议端当成控制码；协议端会还原 |
| URL | 官方可能拒绝含链接的消息（错误码 `40054010`）。开启 `QQBOT_STRIP_URLS=true` 后自动替换为「（链接已省略）」 |

---

## 5. 查询投递结果

### `GET /status/<job_id>`

```bash
curl -H "X-Push-Token: <token>" http://127.0.0.1:8088/status/bcc29d3fd332
```

```json
{
  "ok": true,
  "job_id": "bcc29d3fd332",
  "state": "sent",
  "created_at": 1789871271.96,
  "results": [
    {
      "job_id": "bcc29d3fd332",
      "target": "B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5",
      "source": "10.0.0.5",
      "state": "sent",
      "message_id": "ROBOT1.0_MMAEx2P4...",
      "error": "",
      "attempts": 1,
      "created_at": 1789871271.96,
      "finished_at": 1789871273.25
    }
  ]
}
```

| `state` | 含义 |
| --- | --- |
| `pending` | 还在队列/发送中 |
| `sent` | 全部目标成功 |
| `partial` | 部分成功 |
| `failed` | 全部失败（`error` 里有原因与官方 `trace_id`） |

### `GET /healthz`（无需鉴权）

```json
{
  "ok": true,
  "channel": "official",
  "uptime_sec": 193,
  "targets": 1,
  "stats": {"accepted": 1, "sent": 1, "failed": 0, "duplicate": 0, "rejected": 0, "queue_depth": 0}
}
```

### `POST /reload`

重新读取 `data/groups.json`（新发现的 openid 立即生效，不用重启）。

```json
{"ok": true, "channel": "official", "targets": 1, "names": ["运维告警"]}
```

---

## 6. 官方通道常见错误码

失败时 `error` 字段形如：

```
官方接口返回错误 code=40034105 主动消息失败, 无权限（主动消息无权限：群管理员可能关闭了机器人通知，或该群不支持主动消息） [trace_id=efd80d9a...]
```

| code | 含义与处理 |
| --- | --- |
| `11255` | `group_openid` 不对，或机器人已不在该群 → 重新取号 |
| `11243` / `100016` | AppID/Secret 不对 → 检查 `QQBOT_APPID` / `QQBOT_SECRET` |
| `40034101` / `40054003` | 机器人不是群成员 → 先把机器人拉进群 |
| `40034105` | 该群未开启机器人**主动消息**。用 `qqpush-discover --reply-test` 验证：被动回复成功即说明机器人可用，去群内机器人资料页开启通知/主动发言 |
| `40034100` | 主动消息超频（每群 20 条/分钟、1000 条/天）→ 降频或合并消息 |
| `40054010` | 不允许发送 URL → 开 `QQBOT_STRIP_URLS=true` |
| `40054007` | 消息长度超限 → 调小 `QQBOT_MAX_CHARS` |
| `40054016` | 机器人已下线（需上线，或只能用沙箱群） |
| `50055001` / `50055006` | 官方侧抖动，服务已自动重试 |

服务对**可重试**错误（网络、5xx、频控）自动重试（默认 2 次、指数退避）；
对**致命**错误（如机器人不在群）立即失败，不空转。失败详情写入 `data/failed.ndjson`。

---

## 7. 调用示例

### curl

```bash
TOKEN=your-token
OPENID=B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5

# 文本
curl -X POST http://127.0.0.1:8088/push \
  -H "X-Push-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"group_openid\": \"$OPENID\", \"message\": \"服务已恢复\"}"

# 带标题 + 幂等 ID（上游重试不会刷屏）
curl -X POST http://127.0.0.1:8088/push \
  -H "X-Push-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"group_openid\": \"$OPENID\", \"title\": \"告警\", \"message\": \"磁盘 95%\", \"id\": \"disk-2026-09-20\"}"
```

### Python（标准库即可）

```python
import json
import urllib.request


def push(message: str, *, openid: str, token: str, title: str = "", base="http://127.0.0.1:8088"):
    body = json.dumps({"group_openid": openid, "message": message, "title": title}).encode()
    req = urllib.request.Request(
        f"{base}/push",
        data=body,
        headers={"X-Push-Token": token, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


print(
    push(
        "磁盘使用率 95%",
        openid="B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5",
        token="your-token",
        title="告警",
    )
)
```

### 上游重试建议

带 `id` 字段即可安全重试（窗口内只发一次）：

```bash
id="build-${CI_PIPELINE_ID}-${CI_JOB_ID}"
```

只依据 `202` 判断"已受理"；要确认送达用 `GET /status/<job_id>`。
