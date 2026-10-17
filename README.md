# AI Agent Harness

基于 Python 的本地 AI Agent 运行框架，支持通过 Web 工作台或命令行提交任务，让模型在受控工具、运行预算和结果检查下完成工作。

```text
任务 → 模型决策 → 工具执行 → 结果反馈 → 后续决策 → 最终答复与检查
```

## 功能

- **任务执行**：接入 OpenAI 兼容的 Chat Completions 接口，支持工具调用、有限重试、超时和取消。
- **上下文管理**：加载工作目录根部的 `AGENTS.md`，维护可调整计划、带来源的任务记忆和近期交互历史。
- **文件操作**：读写工作目录内的 UTF-8 文本，以文件哈希检测写入冲突，保留已有编码与换行风格。
- **命令审批**：Web 中逐条批准 Shell 命令，支持执行超时、取消与 Windows 进程树清理。
- **联网读取**：可选 Tavily 搜索与网页正文提取，支持长文分段续读和来源追溯。
- **检查与追踪**：配置结果检查，失败后反馈模型修正；保存完整事件、工具反馈和最终报告。

技术栈：Python 3.12、asyncio、FastAPI、HTTPX、原生 HTML/CSS/JavaScript。Python 后端同时提供页面和 API。

## 快速开始

以下命令在仓库根目录、已初始化 conda 的 PowerShell 中执行。

### 1. 安装环境

```powershell
conda env create -f environment.yml
conda activate harness
```

已有 `harness` 环境时，激活后运行 `python -m pip install -r requirements.txt` 更新依赖即可。

### 2. 配置模型

首次使用时复制配置模板，已有 `.env` 请保留：

```powershell
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
```

在本机 `.env` 中填写：

```dotenv
HARNESS_PROVIDER=openai_compatible
HARNESS_BASE_URL=你的 API 基础地址
HARNESS_MODEL=服务实际支持的模型标识
HARNESS_API_KEY=你的 API 密钥
```

### 3. 启动 Web 工作台

```powershell
python -m harness serve --env-file .env --config config.example.json
```

打开终端给出的地址，默认是 `http://127.0.0.1:8765`。填写已有工作目录和任务后即可运行，页面提供回答、产物、计划、工具轨迹和命令审批。可用 `--port 8766` 更换端口，请使用输出的精确地址，`localhost` 别名会被 Host 校验拒绝。

刷新页面可恢复运行记录；关闭页面不会取消任务，请使用“取消运行”。停止服务使用 Ctrl+C，并等待活动任务及命令进程清理结束。

## 命令行使用

运行任务：

```powershell
python -m harness run --env-file .env --config config.example.json --task "先用工具计算47+58，再将返回值乘以3，报告结果。"
```

文件任务添加 `--workspace "你的工作目录"`。命令行支持文件和可选联网工具，Shell 命令及 Shell 检查通过 Web 审批执行。

验证真实模型的工具调用闭环：

```powershell
python -m harness live-check --env-file .env --config config.example.json
```

成功时输出 `live_check_passed: true`。此命令调用真实模型，会产生 API 用量；配置缺失时明确报错。

## 配置

运行参数见 [config.example.json](config.example.json)，通过 `--config` 指定。模型连接信息优先使用已有进程环境，其次为显式加载的 `.env`、JSON 和默认值；不会自动寻找父目录的 `.env`。修改配置后需重启服务，新任务才会采用新值。

常用默认值：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `max_turns` | 8 | 最后一轮保留给最终答复，不派发模型工具调用 |
| `request_timeout_seconds` | 90 | 单次模型请求时限，单位秒 |
| `total_timeout_seconds` | 360 | 任务总时限，包含工具、审批及重试等待 |
| `approval_timeout_seconds` / `shell_timeout_seconds` | 60 / 60 | 审批等待和单条命令执行上限，单位秒 |
| `recent_history_turns` | 4 | 保留近期完整交互组，规范、计划和任务记忆持续提供 |
| `max_completion_tokens` / `max_output_chars` | 4096 / 16000 | 单次生成 token 上限与输出字符上限，分别约束 |
| `checks` | `[]` | 默认不配置结果检查 |
| `network_enabled` | `false` | 默认关闭联网 |

若模型服务要求 `max_completion_tokens` 参数，将 `completion_token_parameter` 改为 `"max_completion_tokens"`；生成上限数值由 `max_completion_tokens` 配置项控制。

供应商以 `length` 结束响应时，执行器将其识别为生成截断；如尚有轮数，会在原预算内尝试一次精简答复，不重放工具或自动提高配置上限。仍被截断时明确报告预算耗尽。较高的生成上限允许更多输出，也可能增加耗时与用量；已有自定义配置需自行调整并重启服务。

### 联网搜索与网页读取

在本机 `.env` 中填写 `TAVILY_API_KEY`，将启动 JSON 的 `network_enabled` 改为 `true` 后重启。启用后，模型可在预算内调用 `web_search` 和 `read_webpage`；搜索词和待读网址会发送给 Tavily，并产生相应 API 用量。

长正文按文档编号续读，不重复抓取。来源、保存正文和已读区间会落盘；提取成功或读完保存内容不保证原网页完整，引用编号存在也不等于事实已核实。登录、验证码、浏览器交互及 PDF 不在支持保证范围内。

### 结果检查

可在启动 JSON 中合并以下字段，为产物设置检查：

```json
{
  "checks": [
    {"id": "summary_source", "type": "file_contains", "path": "summary.txt", "text": "来源：input.txt"}
  ]
}
```

支持 `file_contains` 和 `shell` 两类检查。模型提出最终答复后，执行器运行配置检查；失败证据反馈模型，在剩余预算内修正。每次 Shell 检查均需独立审批。

无检查时，正常结束为 `unverified`；全部配置检查通过为 `completed`；修正机会耗尽仍未通过为 `verification_failed`。其他终态包括执行失败、取消、预算耗尽和中断。检查通过仅表示配置项通过。

## 运行记录

每次任务保存在 `runs/<run-id>/`，包含配置和规范快照、最新计划与记忆、完整事件 `events.jsonl` 及最终报告 `result.json`。联网启用时还保存来源索引和提取正文。

可在 Web 查看历史，或将下方示例编号替换为实际任务 ID：

```powershell
python -m harness inspect 20261016001 --events
```

自定义记录目录时添加 `--runs-dir "记录目录"`。历史记录用于查看；中断任务不自动续跑，也不复用旧审批。

## 开发与测试

```powershell
python -m unittest discover -s tests -v
node tests/test_web_ui.js
python -m pip check
```

Python 测试使用 `unittest`，页面脚本测试需要 Node.js。这些检查不调用外部模型，部分测试会启动并清理本地服务和子进程。真实模型与完整样例的复现方式见 [交付与复现指南](docs/MVP_DELIVERY.md)。

## 使用边界

- 面向可信本地项目，单用户、单活动任务；服务仅监听 `127.0.0.1`，不支持远程公开部署或多 worker。
- Shell 审批不提供沙箱隔离，批准的命令可访问工作目录外资源；命令结束会清理残余子进程，不支持启动常驻服务。
- 文件工具默认处理不超过 64 KiB 的 UTF-8 文本；哈希和文件锁不能提供恶意外部修改下的完整事务保证。
- 任务记忆限于本次运行；当前不支持跨任务长期记忆、断点续跑、嵌套规范或多 Agent。
- 当前模型响应为非流式；轮数、时限及输入输出预算可能使任务提前结束。

## 文档

- [学习与源码对照指南](docs/GEN1_LEARNING_GUIDE.md)：架构、源码入口、练习与自测。
- [交付与复现指南](docs/MVP_DELIVERY.md)：完整样例、运行方法和验收记录入口。
- [设计与验收文档](docs/)：详细设计、开发记录及验证证据。
