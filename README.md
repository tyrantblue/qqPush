# qqpush

轻量的 QQ 群消息转发服务：**外部系统 → HTTP /push → qqpush → QQ 群**。

默认走**官方 QQ 机器人**通道（AppID/AppSecret），也保留 NapCat/OneBot 通道可切换。

```
外部系统（监控/CI/脚本）
        │  HTTP POST /push
        ▼
   qqpush（本服务）
        │  ① AppID+Secret 换 access_token（自动刷新）
        │  ② POST /v2/groups/{group_openid}/messages
        ▼
   官方 QQ 机器人（api.bot.qq.com）  ──►  QQ 群
        ▲
        └── WebSocket 事件通道：自动获取 group_openid
```

---

## 1. 快速开始（官方机器人通道）

### 1.1 准备凭据

在 [QQ 开放平台](https://q.qq.com/) → 你的机器人 → **开发设置** 里拿：

| 字段 | 用途 | 说明 |
| --- | --- | --- |
| `AppID` | 机器人 ID | 填到 `QQBOT_APPID` |
| `AppSecret` | 换 access_token 的密钥 | 填到 `QQBOT_SECRET`。**不要提交到仓库，也不要贴到聊天里，泄露后请重置** |
| ~~Token~~ | 已废弃 | 官方已改用 access_token，本服务自动获取与刷新（有效期 7200 秒） |

`access_token` 不需要你手动配置：服务会调 `POST /app/getAppAccessToken` 拿，并在过期前自动换新。

### 1.2 起服务（uv）

```bash
uv sync
cp .env.example .env        # 填 QQBOT_APPID / QQBOT_SECRET / QQPUSH_TOKEN
cp data/groups.example.json data/groups.json
uv run qqpush               # 等价于 uv run python -m qqpush
```

或用 Docker（注意带上 uid/gid，否则容器写不了 `./data`）：

```bash
mkdir -p data
cp .env.example .env && cp data/groups.example.json data/groups.json
QQPUSH_UID=$(id -u) QQPUSH_GID=$(id -g) docker compose up -d qqpush
```

### 1.3 取 group_openid（**官方通道必须的一步**）

官方接口发群消息用的不是群号，而是 `group_openid`，而**开放平台面板里看不到它**，只能从事件里拿。
先建立一个关键认知（实测确认）：

> 官方事件里的 `group_id` **就是** `group_openid`，**不是真实群号**。例如同一条事件里
> `group_id` 与 `group_openid` 都是 `B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5`，
> 你配的 `123456789` 在事件里根本不出现。所以无法"按群号匹配"，本服务用**排除法**绑定。

步骤：

1. 把机器人拉进目标群，并在 `data/groups.json` 配好 `groups`（群名 → 群号，群号仅作备注/OneBot 通道用）；
2. 起服务后**在群里 @ 一下机器人**（事件是即时推送的，**服务必须正在运行**，不会补发历史事件）；
3. 服务收到 `GROUP_AT_MESSAGE_CREATE` 后：**只剩一个待绑定群时**自动认领，写入 `group_openids`：

```json
{
  "groups": { "运维告警": 123456789 },
  "group_openids": {
    "运维告警": { "openid": "B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5", "group_number": "" }
  },
  "default_targets": ["运维告警"]
}
```

多个群都没绑定时，服务**不会猜**（避免推错群），而是打印可复制的 `group_openid` 让你手工绑定：
先手工绑一个，剩下的就能继续靠排除法自动认领。

不想跑常驻服务也可以单独取号/自检：

```bash
uv run qqpush-discover                 # 监听事件并打印 group_openid，Ctrl+C 退出
uv run qqpush-discover --reply-test    # 被动回复自检：验证机器人能否在该群发言
```

**主动消息权限**：实测中主动推送曾返回 `40034105 主动消息失败, 无权限`，而同一群**被动回复成功**——
说明这不是配置错误，而是该群的「主动消息」权限/开关未开。在机器人资料页开启通知/主动发言后，
主动推送即可成功（`成功 1/1`）。用 `--reply-test` 可以快速区分这两类问题。

### 1.4 开始推送

```bash
curl -X POST http://127.0.0.1:8088/push \
  -H 'X-Push-Token: change-me-please' -H 'Content-Type: application/json' \
  -d '{"message": "磁盘使用率 95%", "groups": ["运维告警"]}'
```

---

## 2. 接口

> 面向调用方的完整参数、消息格式与错误码说明见 **[docs/API.md](docs/API.md)**。


服务默认监听 `8088`（业务）与 `8089`（仅健康检查，只绑本机）。

鉴权：设置 `QQPUSH_TOKEN` 后所有接口需带 `X-Push-Token: <token>` 或 `Authorization: Bearer <token>`；留空则不校验（**公网务必设置**）。

### `POST /push`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `message` | string \| array | 必填（或用 `messages`）。字符串按文本发送；数组为 OneBot 消息段（官方通道会把非文本段降级为文字） |
| `messages` | array | 批量推送，元素为上面的 `message` |
| `groups` | string \| array | 目标群名；官方通道也接受直接写 `group_openid` 或群号；缺省用 `default_targets` |
| `title` | string | 标题行，形如 `【标题】` |
| `prefix` | string | 固定前缀，缺省用 `QQPUSH_DEFAULT_PREFIX` |
| `id` | string | 幂等 ID，`QQPUSH_DEDUP_TTL` 秒内重复提交只发一次 |
| `source` | string | 来源标记，仅用于日志/失败记录 |
| `markdown` | bool | 是否把 markdown 降级为纯文本，缺省用服务配置 |

```bash
# 多群 + 标题 + 幂等
curl -X POST http://127.0.0.1:8088/push \
  -H 'X-Push-Token: change-me-please' -H 'Content-Type: application/json' \
  -d '{"title": "CI", "message": "**构建失败** 见 https://ci/build/1",
       "groups": ["运维告警", "开发群"], "id": "build-1234"}'
```

返回：

```json
{"ok": true, "job_id": "102e793347d0", "job_ids": ["102e793347d0"],
 "targets": ["B2C3D4E5..."], "chunks": 1, "duplicates": 0, "rejected": null}
```

状态码：`202` 已受理 · `200` 重复消息 · `400` 请求不合法 · `401` 鉴权失败 · `404` 群未配置 ·
`429` 队列已满 · `500` 内部错误。

### 其他接口

| 接口 | 说明 |
| --- | --- |
| `GET /healthz` | 服务状态、当前通道、队列深度、累计统计（无需鉴权） |
| `GET /groups` | 当前通道可推送的目标（官方通道列出群名与 openid） |
| `GET /status/<job_id>` | 查询一次推送的投递结果（`pending`/`sent`/`partial`/`failed`） |
| `POST /reload` | 热加载 `groups.json`（含新发现的 openid），不用重启 |
| `GET /` | 接口清单 |

---

## 3. 官方通道的限制（重要）

官方机器人是**被动优先**的，做「随时主动推送」受这些约束：

| 项 | 限制 |
| --- | --- |
| 主动消息 · 每群 | 20 条/分钟、**1000 条/天** |
| 主动消息 · 机器人维度 | 企业认证 60/分钟、未认证 30/分钟 |
| 被动回复窗口 | 收到 @ 后 5 分钟内可回，每条消息最多回 5 次 |
| 用户/群开关 | 群管理员可关闭机器人通知（`40034105`），关闭后主动消息一律失败 |
| 沙箱 | 机器人未上线时只能在**沙箱群**里收发 |
| 内容 | 可能不允许含 URL（`40054010`），可开 `QQBOT_STRIP_URLS=true` 自动替换 |

所以高频推送（比如每秒多条告警）建议：合并消息后再推、或降低频率；需要完全无限制的主动推送时，
改用 `QQPUSH_CHANNEL=onebot` + NapCat（本仓库同样支持，见下）。

---

## 4. 配置

### `data/groups.json`

```json
{
  "groups":        { "运维告警": 123456789 },
  "group_openids": { "运维告警": { "openid": "自动写入", "group_number": "123456789" } },
  "default_targets": ["运维告警"]
}
```

- `groups`：群名 → 群号（OneBot 通道直接用；官方通道用于把事件里的群号认领成群名）
- `group_openids`：群名 → `{openid, group_number}`，**收到群事件后自动写入**
- 调用方始终用「群名」推送，换通道不用改上游

### 环境变量（完整见 `.env.example`）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `QQPUSH_CHANNEL` | `official` | `official` 官方机器人 / `onebot` NapCat 等 |
| `QQBOT_APPID` / `QQBOT_SECRET` | 空 | 官方机器人凭据 |
| `QQBOT_LISTEN` | `true` | 是否用 WebSocket 监听事件以自动获取 openid |
| `QQBOT_INTENTS` | `33554432` | 事件订阅位，`1<<25` = GROUP_AND_C2C_EVENT |
| `QQBOT_MAX_CHARS` | `1000` | 单条文本上限，超出截断 |
| `QQBOT_STRIP_URLS` | `false` | 自动把 URL 换成「（链接已省略）」 |
| `QQBOT_GROUP_PER_MINUTE` | `18` | 每群每分钟上限 |
| `QQPUSH_HOST` / `QQPUSH_PORT` | `0.0.0.0` / `8088` | 业务监听 |
| `QQPUSH_HEALTH_PORT` | `0` | 独立健康检查端口，`0` 关闭 |
| `QQPUSH_TOKEN` | 空 | 推送密钥，公网必填 |
| `QQPUSH_WORKERS` / `QQPUSH_QUEUE_SIZE` | `4` / `1000` | 发送并发 / 队列长度 |
| `QQPUSH_DEDUP_TTL` | `300` | 幂等去重窗口（秒） |
| `QQPUSH_MARKDOWN` | `true` | markdown 降级为纯文本 |
| `QQPUSH_FAILED_LOG` | `data/failed.ndjson` | 失败消息落盘 |
| `ONEBOT_BASE_URL` / `ONEBOT_TOKEN` | `http://127.0.0.1:3000` / 空 | OneBot 通道用 |
| `ONEBOT_TIMEOUT` / `ONEBOT_RETRIES` | `10` / `2` | 接口超时（秒）/ 重试次数 |
| `QQPUSH_SEND_RATE_PER_SEC` | `5` | 全局每秒发送上限 |

---

## 5. 切换/并用 OneBot 通道（NapCat）

想要无配额、随时主动推送（例如高频告警）时用这条：

```bash
# .env
QQPUSH_CHANNEL=onebot
ONEBOT_BASE_URL=http://napcat:3000
ONEBOT_TOKEN=你的OneBot Token

mkdir -p napcat/config ntqq
NAPCAT_UID=$(id -u) NAPCAT_GID=$(id -g) docker compose --profile onebot up -d
docker logs napcat                       # 取 WebUI 登录 token
# 浏览器打开 http://<宿主机IP>:6099/webui 登录 QQ
# 「网络配置」新增 HTTP 服务器：端口 3000，Token 与 ONEBOT_TOKEN 一致
```

此时 `/push` 完全不变，只是目标从 `group_openid` 变成群号（`groups` 段生效）。

---

### 5.1 用 systemd 常驻（推荐）

```bash
sudo cp deploy/qqpush.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now qqpush
systemctl status qqpush
tail -f /var/log/qqpush.log      # 看事件与投递日志
```

改完 `.env` 或 `data/groups.json`（群映射）后：`sudo systemctl restart qqpush`；
只改群映射时也可以直接 `curl -X POST .../reload` 热加载，不用重启。

## 6. 可靠性设计

- **异步解耦**：`/push` 只解析 + 入队（毫秒级返回），发送在后台 worker 完成
- **重试与退避**：网络错误、5xx、频控码自动重试；致命错误（如机器人不在群）立即失败不空转
- **限速**：全局 QPS + 每群每分钟双档限速，默认卡在官方 20 qpm 以下
- **幂等**：`id` 去重窗口内只投一次；官方通道 `msg_seq` 自增，避免被判重
- **失败落盘**：失败目标与原始消息写 `data/failed.ndjson`（含 `trace_id`，便于找官方排查）
- **优雅退出**：`SIGTERM/SIGINT` 先排空队列再退出（最多 15 秒）
- **事件通道自愈**：WebSocket 断线自动 resume，失败指数退避重连

## 7. 排障

| 现象 | 处理 |
| --- | --- |
| `404 未知的群` | `groups.json` 里还没有该群，或 openid 未获取（先在群里 @ 机器人） |
| `40034101 / 40054003 机器人非群成员` | 先把机器人拉进群；未上线时用沙箱群 |
| `40034105 主动消息无权限` | 该群未开启机器人「主动消息」。用 `qqpush-discover --reply-test` 验证：被动回复能成功就说明机器人可用，只需在群内机器人资料页开启通知/主动发言 |
| `409 群 X 还没有 group_openid` | 服务当时没在运行（事件不补发）。把服务跑起来，再在群里 @ 一次机器人 |
| 事件收到了但没写回映射 | 事件里的 `group_id` 就是 `group_openid`，不含真实群号。多个群同时待绑定时服务不猜，按日志提示手工绑定其中一个 |
| `40034100 主动消息超过频控` | 降低推送频率（每群 20/分钟、1000/天） |
| `40054010 不允许发送 URL` | 开 `QQBOT_STRIP_URLS=true` |
| `11255 请求的资源不存在` | `group_openid` 不对，重新取号 |
| `11243 access_token 校验未通过` | `QQBOT_APPID/SECRET` 不对，或密钥已重置 |
| `100016 invalid appid or secret` | 同上；若曾泄露过 Secret，请在开放平台重置 |
| `/healthz` 里 `channel=official` 但发不出去 | 看日志里的 `code=` 与 `trace_id`，按上表对号入座 |

## 8. 开发

```bash
uv sync
uv run pytest              # 全部用例（含 mock 官方 API / mock OneBot 的端到端测试）
uv run ruff check .
uv run ruff format .
```

## 9. 项目结构

```
src/qqpush/
  __main__.py     入口：装配、信号处理、优雅退出
  app.py          装配：配置 -> 群映射 -> 通道适配器 -> 推送服务
  config.py       环境变量配置 + 群映射表（群号 / group_openid）
  channels.py     通道适配层：official 与 onebot 对上层同一套接口
  qqbot_auth.py   access_token 获取、缓存、自动刷新
  qqbot_api.py    官方 OpenAPI 客户端（发群消息、错误码、限速、重试）
  qqbot_ws.py     WebSocket 事件监听 + group_openid 注册表（自动写回 groups.json）
  onebot.py       OneBot 11 客户端（备选通道）
  discover.py     qqpush-discover：首次取 group_openid
  server.py       HTTP 接口（标准库 ThreadingHTTPServer）
  core.py         队列、worker、去重、失败落盘、任务状态
  message.py      消息段构造、markdown 降级、CQ 转义、分片
  util.py         日志等工具
tests/            单元 + 端到端测试（mock 官方 API 与 OneBot）
```

## 10. 官方文档

- [获取访问凭证（access_token）](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html)
- [发送群聊消息](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html)
- [消息收发概述与频控规则](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)
- [事件订阅 Intents](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/payload.html)
- [WebSocket 方式](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/websocket.html)
- [错误与调试（错误码表）](https://bot.q.qq.com/wiki/develop/api-v2/openapi/error/error.html)
