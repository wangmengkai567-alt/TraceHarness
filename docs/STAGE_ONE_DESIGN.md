# 阶段一设计决策与契约

日期：2026-10-14。范围仅为计划阶段一，不提前实现后续能力。

## 决策

| 内容 | 决策 | 理由 |
| --- | --- | --- |
| 核心 | Python 3.11+，异步执行 | 请求、退避和工具等待可取消、受限 |
| 开发环境 | conda 环境 `harness`，Python 3.12 | 用户明确选择 conda；以 `environment.yml` 定义，不再创建 venv |
| 依赖与启动 | pip 读取 `requirements.txt`；仓库根目录运行 `python -m harness` | 本地源码项目不打包，不安装项目本身，不依赖 setuptools 或已注册命令 |
| 模型协议 | OpenAI 兼容 Chat Completions，非流式 | 一个协议先打通调用，不绑定供应商或模型 |
| HTTP | HTTPX AsyncClient | 可取消的网络等待，不启用隐含重试或重定向 |
| 配置 | 环境变量/显式 `.env` + JSON | 密钥与运行参数分开，不做配置后台 |
| 存储 | 每次运行独立 JSON/JSONL 目录 | 简化部署，事件可供后续界面使用 |
| 后续 Web | FastAPI，阶段三实现 | 与异步 Python 核心对接，本阶段不安装或运行 Web 服务 |
| 工具 | 真实整数加法与乘法 | 验证有依赖的工具调用，不引入文件权限、shell 或搜索 |
| 真实模型 | 用户配置模型和地址 | 保留 Qwen-3.8-flash 意向，不替换或假定其 API 标识 |
| 搜索 | Tavily，后续工具接入 | 用户已明确方向，本阶段不调用搜索 API |

## 契约

- 输入任务：非空文本与已有工作目录。目录本阶段仅作为元数据，不赋予文件工具。
- 消息：system/user/assistant/tool；工具反馈携带 `tool_call_id`。不发送模型内部推理字段。
- `ToolCall`：标识、名称、JSON 参数字符串；工具校验名称和参数，不信任模型输出。
- `ToolResult`：`ok`、`data`、`error`、`truncated`；错误也反馈模型。截断保持 JSON 可解析。
- `ModelReply`：可见文本、工具调用、用量与结束原因。不完整输出或空答案不会标记正常结束。
- 事件：`sequence`、`run_id`、UTC `timestamp`、`type`、`data`；每次追加刷新并落盘。显示本地时间属于界面职责。
- 结果：终态、原因、答案、`verified=false`、轮数/调用数/工具数、用量与耗时。

工具串行执行。不运行文件或 shell；未知工具返回错误。重复调用标识会终止，避免重试重复执行。正式运行仅支持真实 OpenAI 兼容模型，测试替身只存在于 tests，不提供模拟运行入口。

## 执行限制与错误

- 轮数上限约束模型决策轮次；调用数包括重试。每轮工具数量有独立上限。
- 总时长包括请求、工具等待与退避。请求和工具各有独立超时。
- HTTP 408/409/429/500/502/503/504 及传输错误可有限重试；鉴权、参数和协议错误直接结束。
- 只重试模型请求，不重新派发此前工具。指数退避仍受总时间和请求数限制。
- 工具参数错误/超时反馈模型。本阶段不实现阶段五的复杂恢复与计划调整。
- 输入按序列化字符数限制，不等同精确 token 预算；响应受字节和输出字符限制。
- `length` 等不完整结束原因视为失败，避免截断误报正常结束。

取消是协作式的，受控工具必须支持异步取消。请求取消关闭本地连接，不能撤销远端已发生的费用。同步磁盘写入可能有短暂延迟，不承诺硬实时停止。

## 持久化与信息边界

事件按顺序追加，最终结果使用临时文件替换。事件与结果不是跨文件事务，不承诺断电后的完整一致性。

配置不含密钥字段；HTTP 日志只记录安全错误码，不记录错误正文、鉴权头或原始异常。已配置的模型/Tavily 密钥若原样出现在文本中，会在落盘和最终输出时替换。这不是任意敏感信息的通用识别，任务与可见响应仍是本地敏感数据。

日志不自动上传。本阶段没有沙箱、shell、文件工具、规范加载或长期记忆。

## 官方协议参考

- [OpenAI Function Calling](https://developers.openai.com/api/docs/guides/function-calling)
- [Chat Completions 请求格式](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
- [HTTPX 异步接口](https://www.python-httpx.org/async/)
- [HTTPX 超时](https://www.python-httpx.org/advanced/timeouts/)
- [FastAPI 基础接口](https://fastapi.tiangolo.com/tutorial/first-steps/)

这些资料核实了协议和框架能力，不代表 Qwen-3.8-flash 已通过真实验收。
