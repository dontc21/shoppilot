# ShopPilotBot 面试准备

这份材料只描述仓库中已经实现或实际验证过的内容。面试时应明确区分“代码已实现”“自动化测试通过”和“真实平台验收通过”。

## 1. 一分钟项目介绍

ShopPilotBot 是我结合真实店铺场景开发的本地优先 AI 客服程序。它使用 Playwright 控制独立的 Microsoft Edge，从 B站小店客服网页读取当前会话消息；使用 SQLite 做跨重启去重、发送状态和商品知识存储；使用 DeepSeek Tool Calling 查询本地知识库；最后由 Python 独立安全规则输出 `AUTO_REPLY_CANDIDATE` 或 `NEED_REVIEW`。

这个项目的重点不是“让模型随便回复”，而是保证每个自动化动作都有边界：网页 DOM 必须真实验证、不同买家会话必须隔离、未知或高风险问题必须转人工、模型不能直接控制发送、结果不确定时不能盲目重试。

## 2. 当前真实状态

| 功能 | 自动化测试 | 真实验收 |
| --- | --- | --- |
| 持久化 Edge、人工登录 | 配置检查 | B站商家后台验证通过 |
| 客服页面识别 | 已覆盖 | popup 导航和客服页加载通过 |
| 当前会话消息读取 | 已覆盖 | 两条不同买家消息读取通过 |
| 买家/商家方向识别 | 已覆盖核心规则 | 真实双向消息验证通过 |
| SQLite 消息去重 | 含跨重启测试 | 持续轮询和真实重启通过 |
| 固定测试消息发送 | 已覆盖安全门禁 | 自有测试买家收到消息 |
| SQLite 商品知识库 | 已覆盖 | 模拟 JSON 导入与查询通过 |
| DeepSeek Tool Calling | 假模型完整覆盖 | 模拟问题真实 API 验收通过 |
| 自动回复真实买家 | 未开放 | 未验证 |
| Tkinter 人工审核 | 未实现 | 未验证 |

当前完整测试结果是 `37 passed`。真实商品资料尚未导入，真实买家消息也没有发送给 DeepSeek。

## 3. 系统架构

```text
B站客服网页
    │ Playwright，只操作真实 DOM
    ▼
BilibiliShopAdapter
    │ 当前会话 ID + 消息方向 + 消息内容
    ▼
SQLite 消息状态
    │ 去重后的新买家消息
    ▼
CustomerServiceAgent
    │ 第一次 DeepSeek 请求：选择 search_knowledge
    ▼
Python 参数校验 ──→ SQLite 商品 / FAQ / 售后规则
    │ 工具证据
    ▼
第二次 DeepSeek 请求：生成结构化候选回复
    │
    ▼
DecisionRules（独立于模型）
    ├── AUTO_REPLY_CANDIDATE：只有候选资格，不等于发送授权
    └── NEED_REVIEW：未知、高风险、证据不足或模型异常
```

浏览器、Agent 和发送权限是三个分离的层次。当前 Agent CLI 不导入浏览器模块，因此即使模型输出候选回复，也没有调用真实发送的路径。

## 4. 核心源码

### 浏览器与消息

- `src/shoppilotbot/browser.py`
  - `launch_browser()`：使用 `launch_persistent_context()` 和独立 Edge profile。
  - `message_key()`：优先使用平台消息 ID，否则生成组合 SHA-256 指纹。
  - `ProcessedMessageStore.add_if_new()`：利用 SQLite 主键实现原子去重。
  - `OutgoingMessageStore`：记录 `PREPARED`、`SENT`、`UNKNOWN` 和 `ABORTED`，阻止重复发送。
- `src/shoppilotbot/bilibili.py`
  - `BilibiliShopAdapter.read_messages()`：只读取当前打开会话并区分消息方向。
  - `current_conversation_id()`：读取稳定 `data-mid`，不依赖昵称或正文。
  - `send_message()`：发送前后反复校验会话，只点击一次并检查页面结果。
- `src/shoppilotbot/stage2.py`
  - `run_test_send()`：固定正文、DRY_RUN 预览和一次性授权的测试发送流程。

### 知识库与 Agent

- `src/shoppilotbot/knowledge.py`
  - `KnowledgeBase.import_json()`：验证固定 JSON 格式并导入商品、FAQ 和规则。
  - `KnowledgeBase.search_knowledge()`：使用参数化 SQL 查询固定表和列，模型不能执行任意 SQL。
- `src/shoppilotbot/agent.py`
  - `DeepSeekChatClient.complete()`：限制官方 HTTPS 地址、超时、响应大小和输出 Token。
  - `CustomerServiceAgent.answer()`：编排最多一次工具调用和两次模型请求。
  - `_parse_tool_call()`：校验工具名、参数字段、类型和范围。
  - `DecisionRules.evaluate()`：独立检查风险词、证据、批准状态、知识不足、危险承诺和无依据数字。
  - `TokenUsage`：累计两轮请求的输入、输出和缓存 Token。
- `src/shoppilotbot/agent_cli.py`
  - 只允许人工输入模拟问题，输出结构化结果，不连接浏览器。

## 5. 最值得讲的技术难点

### 5.1 Playwright 窗口为什么曾经关闭

一次性检测脚本执行结束后退出 `sync_playwright()` 上下文，清理了 BrowserContext，窗口随之关闭。没有证据表明这是平台安全检测。后来把流程改为长驻轮询，只在 `Ctrl+C` 或异常退出时清理浏览器资源。

### 5.2 客服入口曾停留在 `about:blank`

在人工点击前增加 `popup`、新页面、`framenavigated`、console、pageerror、requestfailed 和 response 监听。后续测试观察到 JavaScript 事件代理创建 popup，主文档请求返回 200 并成功导航。原故障没有复现，所以结论是“根因未确认”，而不是“已修复 Playwright Bug”。

### 5.3 没有稳定消息 ID 时如何去重

首选平台消息 ID。真实 DOM 没有发现稳定消息 ID 时，使用“会话键、正文、平台时间、消息方向、同类消息出现序号”生成 SHA-256。SQLite 主键保证同一指纹只插入一次。这个方案比只用正文可靠，但虚拟滚动、时间缺失或历史排序变化仍可能改变指纹，这是已知限制。

### 5.4 如何防止串会话和重复发送

发送授权绑定“准确会话 ID + 固定正文”，发送前、填写后和点击前都重新读取当前 `data-mid`。SQLite 在点击前预约发送记录；`SENT` 和 `UNKNOWN` 都禁止自动重试。即使页面结果不确定，也宁可交给人工确认，避免重复发两条。

### 5.5 为什么不能只相信模型说“有信心”

模型只负责选择工具和生成候选文本。Python 会验证工具是否真的调用、证据 ID 是否来自本次 SQLite 结果、知识是否获准、回复是否包含危险承诺或无依据数字。退款、投诉、赔偿等问题在请求模型前就转人工。

### 5.6 真实 API 验收发现了什么 Bug

询问模拟资料不存在的“无线充电”时，搜索因为商品名命中返回了材质和容量资料。模型说“资料未提及”，但旧规则仍给出候选资格。根据真实工具轨迹确认根因后，增加“无法确认、未提及、暂无资料”等知识不足门禁和回归测试；相同问题真实复测后变为 `NEED_REVIEW`。

### 5.7 一个小但真实的 Python Bug

一次性授权短语包含中文，直接传给 `secrets.compare_digest()` 会触发非 ASCII 字符串 `TypeError`。异常发生在填写和点击之前，因此没有误发。修复方式是双方先编码为 UTF-8 字节再比较，并增加非 ASCII 回归测试。

## 6. 安全设计清单

- 默认 `SHOPPILOT_DRY_RUN=true`。
- 普通配置开关不能开启批量自动发送。
- 真实发送仅存在于固定测试回复的一次性授权流程。
- Agent 没有浏览器发送能力，候选状态不等于发送授权。
- 退款、赔偿、投诉和订单变更等问题在调用模型前转人工。
- API Key 只从被忽略的 `.env` 读取，不硬编码、不打印。
- Edge profile、运行数据库、日志、DOM 快照和截图均不提交。
- 不调用未经授权的平台内部接口，不绕过验证码或风控。
- 真实买家消息未经店主确认不能发送给外部模型。
- 页面状态或发送结果无法确认时安全停止，不盲目重试。

## 7. 双选会演示建议

优先演示不依赖真实店铺和网络的部分：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m shoppilotbot.agent_cli import
.\.venv\Scripts\python.exe -m shoppilotbot.agent_cli search "星云随行杯 材质"
```

如果网络和 DeepSeek 余额正常，再运行：

```powershell
.\.venv\Scripts\python.exe -m shoppilotbot.agent_cli ask
```

建议依次输入：

1. `星云随行杯是什么材质？`
2. `这个杯子的杯身是什么做的？`
3. `星云随行杯支持无线充电吗？`
4. `我要退款并要求赔偿`

前三个问题可以展示 Tool Calling、SQLite 证据和知识不足转人工；第四个问题展示请求模型前的零 Token 高风险拦截。现场不要运行 `send-test`，除非明确使用自己的测试买家并愿意重新完成一次性授权。

## 8. 常见面试追问与参考回答

### 1. 为什么选择 Playwright，而不是直接调用平台接口？

项目只使用店主有权访问的官方网页，并且没有已获授权的客服内部 API。Playwright 可以复用人工登录并操作真实 DOM，同时避免逆向或调用未经授权的接口。

### 2. 为什么使用持久化 BrowserContext？

它可以把登录状态保存在项目专属 profile 中，减少重复登录。目录与日常 Edge profile 隔离，也被 `.gitignore` 排除。

### 3. 如何找到真正的客服页面？

不假设商家首页就是客服页，而是遍历 `context.pages`，结合真实验证过的 URL 特征和客服 DOM；多个页面同时匹配时直接安全停止。

### 4. 如何适应 iframe 或 popup？

入口诊断监听 popup 和 frame 导航。适配器会在主页面及 frame 中寻找唯一匹配的客服结构，无法唯一确定时不读取或发送。

### 5. 如何判断消息是买家发的还是商家发的？

使用真实 DOM 验证出的方向结构，例如商家消息容器的反向布局类，而不是根据文本内容猜测；并用自有买家和商家各发一条消息做了真实验证。

### 6. 去重为什么用 SQLite？

SQLite 无需额外服务，适合单机 MVP。唯一主键可以原子地判断消息是否首次处理，并且程序重启后仍保留状态。

### 7. 为什么不用消息正文作为唯一键？

买家可能连续发送相同文本。回退指纹还包含会话、时间、方向和出现序号，降低误去重概率。

### 8. 如何防止不同买家串消息？

所有消息和发送授权都绑定会话 ID。发送时使用页面稳定的 `data-mid` 做完全一致校验，不使用昵称或正文定位会话。

### 9. 为什么发送结果有 `UNKNOWN`？

点击后可能因页面卡顿而无法确认结果。此时不能假设失败并重试，否则可能重复发送，所以记录 `UNKNOWN` 并交给人工检查。

### 10. Tool Calling 的完整流程是什么？

第一轮模型只能请求 `search_knowledge`；Python 校验参数后查询 SQLite；工具结果作为 `tool` 消息交给第二轮模型；模型输出 JSON 候选；Python 最后执行独立规则。

### 11. 如何防止模型执行恶意 SQL？

模型只提供查询字符串和数量上限。表名、列名和 SQL 模板写死在 Python 中，用户内容全部使用参数绑定，模型无法提交 SQL 语句。

### 12. 为什么需要两层决策？

LLM 擅长理解自然语言，但不适合作为最终权限控制。模型生成内容后，确定性的 Python 规则再验证证据、风险和格式，从而形成可测试的安全边界。

### 13. API 超时或返回错误怎么办？

捕获超时、网络异常和格式错误，统一返回 `NEED_REVIEW`。不会使用旧回复，也不会因为模型失败而直接发送。

### 14. 如何限制费用？

一次问题最多一次工具调用、两次模型请求；请求有 20 秒超时，输出默认限制 500 tokens、代码硬上限 800；API usage 会汇总输入、输出和缓存 Token。

### 15. 如何处理退款和投诉？

在调用模型之前使用 Python 高风险规则识别，直接返回 `NEED_REVIEW`，避免生成未经确认的退款、赔偿或质保承诺，也节省 Token。

### 16. 真实 API 测试发现的最大价值是什么？

假模型测试证明流程按预期运行，真实模型测试则发现了“有相关商品证据，但证据没有回答具体问题”仍被放行的语义缺口，说明真实验收不能被单元测试完全替代。

### 17. 如何保护隐私？

仓库只包含模拟商品。Cookie、Token、API Key、浏览器 profile、运行数据库和真实 DOM 日志都被忽略。真实买家消息目前没有接入 Agent。

### 18. 为什么没有使用 LangGraph、Redis 或 Celery？

当前是单机、低并发、可演示的 MVP。简单函数、SQLite 和同步流程更容易理解、验证和维护；只有出现明确并发需求时才值得增加基础设施。

### 19. 你如何证明这是自己的开发工作？

仓库提交按真实里程碑组织，开发日志记录了观察证据、未复现问题和真实 Bug；平台适配、去重、发送门禁、知识库及 Agent 编排均在本项目中实现，第三方只使用通用库。

### 20. 下一步最合理的功能是什么？

先实现本地 Tkinter 人工审核队列，把 `NEED_REVIEW` 和候选回复展示给店主；随后才考虑把已批准候选与现有一次性安全发送机制连接。不会直接开放批量自动回复。

## 9. 必须如实说明的限制

- 只监听店主当前打开的会话，尚未自动遍历全部买家。
- 回退消息指纹仍可能受虚拟滚动、时间字段或排序变化影响。
- B站页面使用 CSS Modules，平台更新后定位器可能失效。
- 客服入口早期 `about:blank` 现象没有复现，根因未确认。
- Agent 只用模拟资料验收，没有接收真实买家数据。
- `AUTO_REPLY_CANDIDATE` 只是候选资格，自动发送尚未实现。
- Tkinter 人工审核尚未实现。

面试时承认边界比夸大完成度更有说服力：这个项目已经证明了真实网页收发、持久化去重和 Tool Calling 安全闭环，但仍处在受控 MVP 阶段。
