# RSS NodeSeek 监控机器人

这是一个支持多用户的 NodeSeek RSS 监控机器人。任意 Telegram 用户或群组均可使用，每个用户/群组拥有**独立**的监控规则和偏好设置。

## 功能特性

- **多租户支持**：完全的数据隔离，每个用户/群组只能管理自己的规则。
- **灵活的指令**：支持批量添加/删除、默认模板、正则表达式匹配、排除词等。
- **智能推送**：RSS 更新时，自动向所有命中规则的用户推送通知。

## 快速开始

### 环境变量

- `TG_BOT_TOKEN`: Telegram Bot Token (必填)
- `TG_CHAT_ID`: **管理员**的 Chat ID (必填，仅该 ID 拥有系统管理权限)

### 安装依赖并运行

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 设置环境变量
export TG_BOT_TOKEN=your_bot_token
export TG_CHAT_ID=your_chat_id

# 3. 运行
python3 index.py
```

## Telegram 指令

所有指令支持 BotFather 菜单补全。

### Docker 运行

1. 准备 `.env`（示例）:
   ```
   TG_BOT_TOKEN=your_bot_token
   2. 构建镜像：`docker build -t nodeseek-top-rss .`
   ```
2. 启动容器并挂载数据目录：
   - Linux/macOS: `docker run -d --name ns-top-rss --env-file .env -v $(pwd)/data:/data nodeseek-top-rss`
   - PowerShell: `docker run -d --name ns-top-rss --env-file .env -v ${PWD}/data:/data nodeseek-top-rss`

### Docker Compose

1. 准备 `.env` 同上，并确保 `docker-compose.yml` 存在（已提供）。
2. 启动：`docker compose up -d`
3. 查看日志：`docker compose logs -f`

### 规则管理

- **/add [clean|clean-i|clean-e] 词 1 [词 2...] [+必含] [-排除]**
  - **批量添加**：`/add mk hep dmit` (同时监控 mk, hep, dmit)
  - **带条件添加**：`/add mk hep +收 +出一 -求` (同时监控 mk, hep，且都必须包含[收/出一]之一，不能含[求])
  - **清理后添加**：`/add clean mk` (清除 mk 的所有原有必含/排除条件)
  - **混合标记**：`clean` (清空所有), `clean-i` (清空必含), `clean-e` (清空排除)
- **/del 词 1 [词 2...]**

  - **批量删除**：`/del mk hep` (同时删除这几个监控规则)

- **/list**

  - 查看您当前的所有监控规则、默认模板及全局屏蔽词。

- **/include 词 1 [词 2...]** / **/exclude 词 1 [词 2...]**

  - 设置**默认模板**。
  - 示例：`/include 收`，之后执行 `/add mk` 时会自动给 mk 加上 `+收`。
  - 仅影响**新创建**且未使用 `clean` 标记的规则。

- **/block 词 1 [词 2...]** / **/unblock 词 1 [词 2...]**
  - 管理您的**全局屏蔽词**（对您的所有规则生效）。
- **/blocklist**
  - 查看全局屏蔽列表。

### 个人设置

- **/setsummary on/off**: 是否匹配摘要（默认开启）。
- **/setfullword on/off**: 是否完整词匹配（例如 `mk` 不匹配 `mks`）。
- **/setregex on/off**: 是否开启正则模式。
- **/status**: 查看状态及规则数量。

### 管理员指令 (仅限 TG_CHAT_ID)

- **/setinterval min max**: 设置检测间隔（秒），例如 `/setinterval 30 60`。

## 常见问题

1.  **怎么让群组也能用？**
    把机器人拉入群组，并给予发送消息权限即可。群组发送的指令仅影响该群组的订阅。
2.  **收不到通知？**
    请检查 `/list` 确认规则，或查看 `/blocklist` 全局屏蔽。
    如果是管理员，请检查日志看 RSS 是否正常抓取。

## 维护

日志文件位于 `data/monitor.log`。程序会自动处理内存泄露和长时间僵死问题。
