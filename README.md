# ShopPilotBot

ShopPilotBot 是一个本地优先的 Python AI 电商客服项目，用于真实店铺实验、计算机本科生学习和技术面试展示。当前接入目标是 **B站小店（哔哩哔哩商家后台）**。

项目坚持两个原则：平台 DOM 必须经过真实页面验证后才能使用；任何自动发送能力都必须在店主测试和明确启用后才可开放。当前版本保持 `DRY_RUN`，不会向买家发送消息。

## 项目状态

“已编写”“自动化测试通过”和“真实平台验证”是三个不同状态：

| 功能 | 已编写 | 自动化测试 | 真实 B站验证 |
| --- | --- | --- | --- |
| Playwright 启动独立持久化 Edge | 是 | 配置检查 | 是 |
| 手动登录并复用本地用户目录 | 是 | 不适合单元测试 | 是 |
| 从 `context.pages` 识别客服标签页 | 是 | 是 | 客服页地址已验证 |
| 客服入口生命周期与网络诊断 | 是 | 脱敏函数已测试 | 是 |
| 当前会话 DOM 探测 | 是 | 是 | 是 |
| 买家/商家消息区分 | 是 | 部分 | 买家消息读取成功，方向 DOM 已确认 |
| SQLite 消息去重 | 是 | 是，含跨重启测试 | 持续轮询通过，跨重启未验收 |
| 单条测试消息安全发送 | 是 | 是 | 是，买家端已收到 |
| 批量/自动回复 | 否 | 不适用 | 未开放 |
| DeepSeek Agent / Tool Calling | 否 | 否 | 未验证 |
| 商品知识库 | 否 | 否 | 未验证 |
| Tkinter 人工审核 | 否 | 否 | 未验证 |

截至 2026-10-14，本地测试实际结果为 `19 passed`。真实平台已完成跨重启去重、买家/商家方向识别，以及一条固定测试回复的发送闭环。

## 技术栈

- Python 3.11+
- Playwright + Microsoft Edge
- SQLite（Python 标准库）
- Pytest
- 计划接入：DeepSeek API、Tkinter

运行依赖只有 Playwright 和 python-dotenv；测试依赖为 Pytest。仓库不包含第三方电商机器人业务代码，也不复制平台未公开接口。

## 当前架构

```text
Microsoft Edge（独立持久化配置）
        │
        ▼
Playwright BrowserContext
        │ 从 context.pages 选择已验证客服页
        ▼
BilibiliShopAdapter（只读当前会话）
        │
        ▼
IncomingMessage
        │
        ├── 平台消息 ID ──────────────┐
        └── 组合 SHA-256 回退指纹 ────┤
                                      ▼
                            SQLite 去重状态
                                      │
                                      ▼
                              本地终端输出
```

计划中的完整流程是：

```text
新买家消息 → SQLite 去重 → DeepSeek Tool Calling → 本地知识库
                                      │
                         安全规则与证据检查
                              ┌───────┴────────┐
                              ▼                ▼
                         AUTO_REPLY       NEED_REVIEW
                         （未开放）       Tkinter 审核
```

## 安装

在 Windows PowerShell 中：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
New-Item -ItemType Directory -Force .runtime | Out-Null
Copy-Item config/selectors.example.json .runtime/bilibili_selectors.json
```

默认使用 `channel="msedge"` 和项目专属的 `edge_bilibili_profile/`。它不会读取或修改日常使用的 Edge 用户配置。

## 阶段一：只读客服消息

首次确认真实消息 DOM：

```powershell
.\.venv\Scripts\python.exe -m shoppilotbot.cli probe-chat
```

程序会：

1. 从 `context.pages` 查找此前诊断确认过的客服主文档。
2. 如果客服页没有恢复，等待店主手动从商家后台进入。
3. 明确提示店主使用自己的测试买家账号发送“你好”。
4. 只保存该测试文本周围的脱敏结构和不含正文的 DOM 骨架。
5. 等待人工确认定位器，然后在同一进程内转入当前会话只读监听。

仓库中的示例定位器来自 2026-10-14 的真实页面验证。复制到 `.runtime/` 后可直接运行：

```powershell
.\.venv\Scripts\python.exe -m shoppilotbot.cli listen
```

程序启动时会把当前可见消息记为基线。基线建立后发送的新消息才会作为新消息输出。监听持续到店主按 `Ctrl+C`。

通用脱敏 DOM 检查和客服入口诊断命令分别为：

```powershell
.\.venv\Scripts\python.exe -m shoppilotbot.cli inspect
.\.venv\Scripts\python.exe -m shoppilotbot.cli diagnose-entry
```

这些命令不会自动点击客服入口、刷新页面或发送消息。

## 阶段二：安全测试发送

先以只读方式验证跨重启去重和消息方向：

```powershell
.\.venv\Scripts\python.exe -m shoppilotbot.stage2 verify
```

再启动专用测试发送流程：

```powershell
.\.venv\Scripts\python.exe -m shoppilotbot.stage2 send-test
```

`send-test` 只允许固定回复 `ShopPilotBot 测试回复，请忽略。`。程序先执行 `DRY_RUN` 预览，然后生成包含脱敏会话标识和随机码的一次性授权短语。只有店主原样确认后，才会对当前 `data-mid` 完全一致的会话填写正文并单击一次。

发送尝试会先写入 SQLite，但只保存会话哈希、正文哈希和状态，不保存原始会话 ID 或正文。页面出现新增的同文本商家消息时记录 `SENT`；点击后无法确认时记录 `UNKNOWN`，两种状态均禁止自动重试。相同会话和正文的重复请求也会在点击前被阻止。

## 去重设计与限制

`ProcessedMessageStore` 只持久化消息哈希、脱敏会话键和首次观察时间，不保存买家名称或聊天正文。

- 首选：`conversation_key + 平台消息 ID`。
- 回退：`conversation_key + 正文 + 平台时间 + 消息方向 + 同类消息出现序号`，再计算 SHA-256。

当前真实 DOM 未发现稳定的平台消息 ID，因此使用回退方案。它不是仅按正文去重，但如果虚拟滚动改变历史消息的可见顺序、平台时间缺失或格式变化，仍可能出现误判。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

当前自动化测试覆盖：

- 文本规范化和消息指纹。
- 不同会话隔离。
- SQLite 重启后的持久化去重。
- 已验证客服 URL 的识别。
- 日志 URL/Token 脱敏。
- 一次性发送授权与会话绑定。
- SQLite 发送状态和重复发送保护。
- 含中文授权短语的安全比较。

## 已知问题与限制

- 曾观察到客服入口打开的新标签页停留在 `about:blank`。加入事件监听后再次测试时，popup、主 frame 导航和客服页面均正常加载，原问题未复现，根因尚未确定，不能视为已修复。
- 2026-10-14 已真实验证当前会话列表、正文、平台时间和消息方向定位器，但尚未发现稳定平台消息 ID；CSS Modules 类名可能随平台发布变化。
- 同一运行进程和真实浏览器重启后的消息去重均已验证。
- 当前只读取店主已经打开的会话，不主动遍历或切换所有买家。
- 平台页面更新可能导致定位器失效；页面状态无法确认时必须停止处理。
- `UNKNOWN` 分支已有代码保护，但尚未在真实平台故意制造发送结果不确定场景。
- 尚未实现 Agent、知识库、人工审核和自动回复；真实发送目前只开放专用命令中的一次性固定测试回复。

详见 [开发日志](docs/development-log.md)。

## 安全设计

- 全局默认并保持 `SHOPPILOT_DRY_RUN=true`；关闭该变量也不能开启批量发送。
- 真实发送只能由专用 `send-test` 命令的一次性、会话绑定授权放行。
- 不绕过验证码、风控或访问控制，不调用未经授权的内部接口。
- Cookie、Token、登录状态和买家信息不发送到外部模型或调试服务。
- 浏览器配置、`.env`、SQLite、日志、DOM 探测结果和截图均由 `.gitignore` 排除。
- DOM 探测不保存聊天正文、表单值、Cookie、URL 参数或动态属性值。
- 登录失效、会话不唯一或页面结构变化时停止处理并提示人工。

推送前执行：

```powershell
git status --short
git diff --cached --name-only
git diff --cached
git ls-files -- ".env" "*.db" "*.sqlite3" "*.log" "*.jsonl" `
  "edge_bilibili_profile/**" "edge_bot_profile/**" `
  "browser_profile/**" ".browser_profile/**" ".runtime/**"
```

最后一条命令应无输出。

## 个人开发与第三方依赖

本仓库中的业务流程、B站页面适配、诊断、脱敏和去重代码均为本项目自行编写。早期曾尝试接入拼多多网页，随后按实际需求切换到 B站小店；拼多多平台配置和定位代码未保留在当前基线中。

项目只使用通用开源依赖：Playwright、python-dotenv 和 Pytest。依赖代码不复制进仓库，具体版本范围见 `pyproject.toml`。
