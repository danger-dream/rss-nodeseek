# RSS NodeSeek 监控机器人

一个通过 Telegram 推送的 NodeSeek RSS 监控脚本，支持关键词/排除词、摘要匹配、正则与完整词匹配、智能重启与状态查询。

## 功能特性
- 关键词、排除关键词管理，支持摘要/正文匹配、完整词匹配、正则匹配可开关。
- 去重通知并限量保存，命中时推送富文本通知。
- Telegram 指令远程管理：增删关键词/排除词、调整检测间隔、匹配开关、通知上限、查看状态。
- 智能重启策略：运行超过 24 小时、内存超过总内存 30%（下限 400MB，上限 2000MB）、或连续错误 ≥15 自动重启。
- 日志同时输出到 `data/monitor.log` 和 stdout，便于前台/容器查看。

## 快速开始
### 安装依赖并运行
```bash
pip install -r requirements.txt

# 设置环境变量（至少需要）
export TG_BOT_TOKEN=your_bot_token
export TG_CHAT_ID=your_chat_id

# 运行
python3 rss_main.py
```
Windows 请使用 `set` 设置环境变量或修改 `.env` 文件后运行。

### Docker 运行
1. 准备 `.env`（示例）:
   ```
   TG_BOT_TOKEN=your_bot_token
   TG_CHAT_ID=your_chat_id
   TZ=Asia/Shanghai
   ```
2. 构建镜像：`docker build -t rss-nodeseek .`
3. 启动容器并挂载数据目录：
   - Linux/macOS: `docker run -d --name rss-bot --env-file .env -v $(pwd)/data:/data rss-nodeseek`
   - PowerShell: `docker run -d --name rss-bot --env-file .env -v ${PWD}/data:/data rss-nodeseek`

### Docker Compose
1. 准备 `.env` 同上，并确保 `docker-compose.yml` 存在（已提供）。
2. 启动：`docker compose up -d`  
3. 查看日志：`docker compose logs -f`

## Telegram 指令
- `/add 关键字`：添加关键词；若关键词在排除列表会提示冲突。
- `/del 关键字`：删除关键词。
- `/list`：查看关键词与排除关键词。
- `/block 关键字`：添加排除关键词；与关键词列表冲突会提示。
- `/unblock 关键字`：删除排除关键词。
- `/blocklist`：查看排除关键词列表。
- `/setsummary on/off`：是否匹配摘要/正文（默认 on）。
- `/setfullword on/off`：是否完整词匹配（默认 off）。
- `/setregex on/off`：是否将关键词视为正则（默认 off）。
- `/setinterval min max`：设置检测间隔秒，示例 `/setinterval 30 60`。
- `/setnotifylimit N`：通知去重上限（0 表示不限制）。
- `/status`：查看运行时长、内存占用、关键词数量、最后检测时间/错误、匹配设置、重启策略、累计检测次数。
- `/help`：查看帮助。

> 支持 BotFather 菜单点击（无参数时会提示输入）。

## 配置说明
- 配置文件：`data/config.json`（启动时若不存在会自动生成，旧配置会自动补全字段）
  ```json
  {
      "keywords": [],
      "exclude_keywords": [],
      "notified_entries": {},
      "settings": {
          "match_summary": true,
          "full_word_match": false,
          "regex_match": false,
          "check_min_interval": 30,
          "check_max_interval": 60,
          "max_notified_entries": 50
      },
      "telegram": {
          "bot_token": "",
          "chat_id": ""
      }
  }
  ```
- 环境变量：`TG_BOT_TOKEN`、`TG_CHAT_ID` 必填；其他运行参数通过 Telegram 指令或直接编辑 `config.json`。

## 运行机制
- 每次检测前会重新加载配置；RSS 命中后发送富文本通知（包含标题/关键词/作者/链接）。
- 排除词优先于关键词；支持标题与摘要匹配（可关）。
- 去重使用帖子 ID+作者归一化；去重记录数量受 `max_notified_entries` 限制。
- 智能重启：运行 >24h、内存 >30% 总内存（400–2000MB 范围）、或连续错误 ≥15 会自动重启以防泄漏/卡死。

## 日志
- 文件：`data/monitor.log`
- 控制台：stdout 同步输出

## 依赖
- `requests`
- `feedparser`
- `psutil`

## 常见问题
- **收不到指令**：确保未配置 webhook（程序会自动 `deleteWebhook`），并且 `TG_BOT_TOKEN` / `TG_CHAT_ID` 正确。
- **未命中通知**：检查关键词/排除词、摘要匹配开关、正则合法性（非法正则会在日志提示）。
- **重启频繁**：可能是内存占用或长时间运行触发智能重启，查看 `/status` 或日志确认原因。

## 开发/测试
建议运行一次语法检查或直接启动验证：
```bash
python -m py_compile rss_main.py
python rss_main.py
```
