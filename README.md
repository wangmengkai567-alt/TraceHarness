# AI Agent Harness

基于 Python 的本地 AI Agent 运行框架，支持通过 Web 工作台或命令行提交任务，让模型在受控工具、运行预算和结果检查下完成工作。

```text
任务 → 模型决策 → 工具执行 → 结果反馈 → 后续决策 → 最终答复与检查
```

## 功能

- **任务执行**：接入 OpenAI 兼容的 Chat Completions 接口，支持工具调用、有限重试、超时和取消。
- **上下文管理**：加载工作目录根部的 `AGENTS.md`，维护可调整计划、带来源的任务记忆和近期交互历史。
- **会话记忆**：Web 使用 Redis 保存完整交互和状态，同会话的新任务可使用近期历史；过期、容量和存储故障明确反馈。
- **任务澄清**：Web 任务缺少必要信息时可提问，用户回答后在同一任务中继续，不重放工具或重置预算。
- **文件操作**：读写工作目录内的 UTF-8 文本，以文件哈希检测写入冲突，保留已有编码与换行风格。
- **隔离命令**：Web 中的 Shell 在离线 Docker 容器执行；限定只读命令自动放行，其余允许的命令逐条审批，支持超时、取消及容器清理。
- **联网读取**：可选 Tavily 搜索与网页正文提取，支持长文分段续读和来源追溯。
- **检查与追踪**：配置结果检查，失败后反馈模型修正；保存完整事件、工具反馈和最终报告。

技术栈：Python 3.12、asyncio、FastAPI、HTTPX、原生 HTML/CSS/JavaScript。Python 后端同时提供页面和 API。

## 快速开始

以下命令在仓库根目录的 PowerShell 中执行。先安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)，确保 `uv --version` 可用；无需激活环境。

### 1. 安装环境

```powershell
uv sync --locked
```

首次执行会创建 `.venv` 并安装 `uv.lock` 锁定的依赖，Python 限定为 3.12，`.python-version` 固定使用 3.12.13；项目直接从源码运行，不打包安装。后续同步仍用 `uv sync --locked`。修改依赖使用 `uv add` / `uv remove`，同时提交 `pyproject.toml` 与 `uv.lock`。不要使用 `--active` 指向其他环境。

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

### 3. 准备本地 Redis

先启动 Docker Desktop 的 Linux 引擎，再执行：

```powershell
uv run --locked python redis/setup.py
docker compose -f redis/compose.yaml up -d
```

首次准备会为本项目生成独立凭据：应用密码追加到本机 `.env`，Redis ACL 和管理凭据保存在被忽略的 `.redis/`；已有模型配置保留，凭据不打印。再次执行保留现有凭据；不匹配时明确报错，需手动核对。Redis 使用固定镜像摘要，绑定 `127.0.0.1:16379`，与其他项目实例分开，数据保存在 Docker 命名卷 `harness-sessions_data`。

Redis 启用 AOF 每秒同步、256 MiB 数据内存上限和 `noeviction`；异常掉电可能丢失约一秒数据。容器内存上限 512 MiB，留出运行开销。停止时保留数据：

```powershell
docker compose -f redis/compose.yaml stop
```

不要使用删除数据卷的命令清理日常服务。应用不会自动启动 Redis，Redis 故障也不会转用 JSON 或内存保存会话。

### 4. 启动 Web 工作台

```powershell
uv run --locked python -m harness serve --env-file .env --config config.example.json
```

打开终端给出的地址，默认是 `http://127.0.0.1:8765`。填写已有工作目录，点击“新建会话”，再填写任务并开始运行，页面提供回答、产物、计划、工具轨迹和命令审批。可用 `--port 8766` 更换端口，请使用输出的精确地址，`localhost` 别名会被 Host 校验拒绝。

刷新页面可恢复运行记录；关闭页面不会取消任务，请使用“取消运行”。停止服务使用 Ctrl+C，并等待活动任务及命令进程清理结束。

## 命令行使用

运行任务：

```powershell
uv run --locked python -m harness run --env-file .env --config config.example.json --task "先用工具计算47+58，再将返回值乘以3，报告结果。"
```

文件任务添加 `--workspace "原始材料目录" --input-file input.txt`；多个文件重复 `--input-file`。仅复制列出的相对路径，根 `AGENTS.md` 自动纳入。文件工具和检查均操作 `runs/<run-id>/workspace`，不自动修改原目录。Web 中每行填写一个相对文件路径。命令行支持文件和可选联网工具，Shell 命令及 Shell 检查仅在 Web 开放，并应用同一命令策略。

验证真实模型的工具调用闭环：

```powershell
uv run --locked python -m harness live-check --env-file .env --config config.example.json
```

成功时输出 `live_check_passed: true`。此命令调用真实模型，会产生 API 用量；配置缺失时明确报错。

固定诊断场景与事件证据检查位于 [`diagnostics.py`](harness/diagnostics.py)，CLI 负责调用和展示，普通任务不会自动运行诊断。计算工具实现位于 [`arithmetic.py`](harness/arithmetic.py)，通用工具反馈长度限制位于 [`tool_results.py`](harness/tool_results.py)。

## Docker CLI 位置

后端仍运行在 Windows 宿主。当前机器的固定 Docker CLI 是 `C:\Program Files\Docker\Docker\resources\bin\docker.exe`；在启动项目的同一 PowerShell 中设置：

```powershell
$env:Path = "$env:USERPROFILE\.local\bin;C:\Program Files\Docker\Docker\resources\bin;$env:Path"
uv --version
uv run --locked docker --version
```

本机 uv 位于 `C:\Users\Dell\.local\bin\uv.exe`。PATH 设置仅作用于当前终端及其子进程；程序不自动搜索其他安装路径。首次使用 Shell 前，启动 Docker Desktop 的 Linux 引擎，在仓库根目录执行：

```powershell
docker build --tag harness-sandbox:1 --file sandbox/Dockerfile sandbox
```

镜像固定为 Python 3.12.13，仅提供标准库；Dockerfile 固定官方基础镜像摘要。任务中不拉取镜像或安装依赖。缺少镜像、Docker 不可达或限制不匹配时明确失败。每条命令使用独立非 root 容器、无网络、只读根目录、1 CPU、512 MiB 内存、64 个进程和 64 MiB 临时目录。工作副本的容量检查不是磁盘硬配额。

## 配置

运行参数见 [config.example.json](config.example.json)，通过 `--config` 指定。模型连接信息优先使用已有进程环境，其次为显式加载的 `.env`、JSON 和默认值；不会自动寻找父目录的 `.env`。修改配置后需重启服务，新任务才会采用新值。

常用默认值：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `max_turns` | 8 | 最后一轮保留给最终答复，不派发模型工具调用 |
| `request_timeout_seconds` | 90 | 单次模型请求时限，单位秒 |
| `total_timeout_seconds` | 360 | 任务总时限，包含工具、审批及重试等待 |
| `approval_timeout_seconds` / `shell_timeout_seconds` | 60 / 60 | 审批等待和单条命令执行上限，单位秒 |
| `redis_port` | 16379 | Redis 固定连接本机 127.0.0.1，应用账号 harness |
| `session_ttl_seconds` | 604800 | 最后一次成功更新后 7 天过期，读取不续期 |
| `max_session_groups` / `max_session_bytes` | 100 / 1048576 | 完整交互组数和序列化 UTF-8 字节上限 |
| `user_wait_timeout_seconds` | 120 | 单次用户澄清等待上限，同时受剩余总时限约束 |
| `recent_history_turns` | 4 | 摘要后保留的近期完整交互组数；阈值以下保留全部未摘要组 |
| `model_context_window` | 1000000 | 当前 Qwen3.8-Flash 的 token 窗口；更换模型须显式核对 |
| `auto_compact_token_limit` | 12000 | token 触发阈值，实际取配置值与模型窗口 90% 的较小值；null 表示仅用 90% |
| `tokenizer_encoding` | o200k_base | 请求前对消息及工具声明做 token 估算；供应商实际 usage 单独记录 |
| `max_summary_chars` | 2000 | 累计摘要 JSON 的字符上限，与 token 阈值独立 |
| `max_completion_tokens` / `max_output_chars` | 4096 / 16000 | 单次生成 token 上限与输出字符上限，分别约束 |
| `checks` | `[]` | 默认不配置结果检查 |
| `network_enabled` | `false` | 默认关闭联网 |

若模型服务要求 `max_completion_tokens` 参数，将 `completion_token_parameter` 改为 `"max_completion_tokens"`；生成上限数值由 `max_completion_tokens` 配置项控制。

供应商以 `length` 结束响应时，执行器将其识别为生成截断；如尚有轮数，会在原预算内尝试一次精简答复，不重放工具或自动提高配置上限。仍被截断时明确报告预算耗尽。较高的生成上限允许更多输出，也可能增加耗时与用量；已有自定义配置需自行调整并重启服务。

### 联网搜索与网页读取

程序默认关闭联网；当前 `config.example.json` 显式设置 `network_enabled=true`。使用该示例即选择启用联网，须在本机 `.env` 中填写 `TAVILY_API_KEY`；不需要联网时将此项设为 `false`。修改后重启。启用后，模型可在预算内调用 `web_search` 和 `read_webpage`；搜索词和待读网址会发送给 Tavily，并产生相应 API 用量。

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

支持 `file_contains` 和 `shell` 两类检查。模型提出最终答复后，执行器运行配置检查；失败证据反馈模型，在剩余预算内修正。Shell 检查与普通命令共用策略；只有完整识别的只读命令自动放行，其他允许的命令每次独立审批。

无检查时，正常结束为 `unverified`；全部配置检查通过为 `completed`；修正机会耗尽仍未通过为 `verification_failed`。其他终态包括执行失败、取消、预算耗尽和中断。检查通过仅表示配置项通过。

### 运行中回答问题

Web 模型任务提供 `ask_user`：缺少必要信息时，页面显示“需要补充信息”和回答框；提交文本后沿用原 Run、计划、记忆和工具结果继续执行。信息充分时无需提问。每次只有一个文本问题和一个回答，问题最多 2000 字符，回答最多 4000 字符；序列化后的回答还须符合工具输出上限，超过时明确拒绝，不静默截断。

单次等待默认最多 120 秒，计入任务总时限。等待期间不请求模型，刷新恢复同一个问题；重复回答、问题标识不符、过期、取消或终态后提交均拒绝。等待超时以 `budget_exceeded` 结束，原因是 `user_wait_timeout` 或 `total_timeout`。回答不批准 Shell、不改变配置检查，也不重新开始预算。CLI 不声明此工具。

提问必须是该轮唯一工具，混合批次在执行前整批拒绝。问题及回答写入 `question.json` 和事件日志，`user_answer_received` 记录接收，`user_answer_used` 关联回答首次进入的后续模型请求。服务重启后未完成 Run 显示中断，不恢复等待或旧操作。验收与复现见 [阶段 2 交付记录](docs/STAGE_TWO_CLARIFICATION_ACCEPTANCE.md)。

## Redis 会话与继续执行

选择历史任务，点击“在此会话继续”，填写新的目标后启动新的 Run。新 Run 重新加载当前项目根规范、重新建立计划和预算，在新工作副本操作；旧计划及任务记忆仅作为历史资料。历史产物不会自动复制到新工作副本，如需使用，先明确取回并列入新任务材料。

一个会话最多保存 100 个完整交互组，序列化总量最多 1 MiB，包含状态和运行关联。一个组由模型回复及其全部工具反馈组成，不等于一次 Run。超过限额明确停止追加，不偷偷删除早期内容；7 天没有成功更新的会话仍会自然过期。Redis 总容量不足时拒绝新增写入，不提前淘汰尚未过期的会话。

阶段 4 根据 token 阈值生成一份累计摘要：低于阈值时发送全部未覆盖完整组；达到阈值后，使用当前模型压缩较早的完整组，保留最近 `recent_history_turns` 组，工具调用与反馈不会拆开。Redis 原始组不删除，摘要内容、版本和覆盖序号一次原子提交。新用户指令优先于摘要；摘要是历史资料，不是权限或检查通过证明，也不会写入长期记忆。

当前模型窗口为 1,000,000 tokens，默认显式压缩阈值为 12,000 tokens，有效阈值不超过模型窗口的 90%；输入和输出预留合计不得超过窗口的 95%。请求前使用配置的 tokenizer 对序列化消息、规范、状态、摘要及工具声明计数，明确记为估算；供应商返回的实际 token 用量另行记录，缺失时为未知。独立的 `max_input_chars=64000` 仍生效，不能用字符数冒充 token 数。Qwen 官方提供的请求前估算编码为 `o200k_base`，换模型时必须同时核对窗口和编码。

首次使用 tiktoken 编码可能需要下载其编码数据，可在服务启动前运行 `uv run --locked python -c "import tiktoken; tiktoken.get_encoding('o200k_base')"` 准备缓存。编码不可用时明确失败，不改用字符比例估算。

摘要及其重试占用原模型调用次数、生成上限和总时限，每次请求前至少预留一次最终回答调用。摘要格式、来源、长度、存储或预算失败时停止，未确认提交前不推进覆盖；不裁掉必要信息或继续使用旧摘要执行。近期组本身已占满时不会拆组强行缩减，超过硬输入限制则明确结束。Web 的“项目上下文”可查看摘要版本、覆盖范围和 token 估算，轨迹保存压缩前后字符数、token 估算、实际 usage 和耗时。详细设计与验收见 [阶段 4](docs/STAGE_FOUR_CONTEXT_ACCEPTANCE.md)。

会话不会恢复旧命令、审批或待回答问题。被取消或中断的半组不进入模型协议历史；可能已经发生的操作以只读的“结果未知”说明保留。重启应用后可以读取仍存在的 Redis 会话并明确发起新 Run，旧 Run 仍为中断。Redis 不可达、鉴权失败、数据损坏、会话不存在或过期分别反馈，不静默创建替代会话。

Web API：`POST /api/sessions` 接收 `{"workspace":"项目目录"}`，`GET /api/sessions/{session_id}` 读取会话信息。模型任务 `POST /api/runs` 必须带 `session_id` 和新生成的 32 位十六进制 `request_id`；重复提交相同请求返回原 Run，复用同一 ID 修改目标或材料会被拒绝。页面会保留结果未确认时的请求 ID。原 `/api/session` 仍只负责同源访问令牌。

CLI `run` 保持独立单次任务，不读写 Redis 会话。命令容器不接收 Redis 凭据。JSON/JSONL 和 `session.json` 是只读执行证据，不是备用会话数据库。设计及验收见 [阶段 3 记录](docs/STAGE_THREE_SESSIONS_ACCEPTANCE.md)。

## 运行记录

每次任务保存在 `runs/<run-id>/`，包含配置和规范快照、最新计划与记忆、完整事件 `events.jsonl` 及最终报告 `result.json`，以及 `workspace/` 工作副本、`sandbox.json` 容器归属/清理记录和 `workspace_manifest.json` 文件差异。产物页标明新增、修改、删除及采集完整性，允许取回校验通过且未变化的普通文件。联网启用时还保存来源索引和提取正文。

可在 Web 查看历史，或将下方示例编号替换为实际任务 ID：

```powershell
uv run --locked python -m harness inspect 20261016001 --events
```

自定义记录目录时添加 `--runs-dir "记录目录"`。历史记录用于查看；中断任务不自动续跑，也不复用旧审批。

## 开发与测试

```powershell
uv run --locked python -m unittest discover -s tests -v
node tests/test_web_ui.js
uv pip check --python .venv/Scripts/python.exe
```

Python 测试使用 `unittest`，页面脚本测试需要 Node.js。Web/会话检查需要先按上文启动本项目 Redis 并生成凭据；它们使用真实 Redis。测试管理凭据仅用于本机故障与容量检查，应用账号仍受 ACL 限制。这些检查不调用外部模型；Shell/HTTP 集成检查需要上述 Docker 镜像和运行中的 Linux 引擎，测试会清理其创建的服务及容器。依赖检查命令中的解释器路径对应 Windows 项目环境；Node.js 需单独安装。真实模型与完整样例的复现方式见 [交付与复现指南](docs/MVP_DELIVERY.md)。

## 使用边界

- 面向可信本地项目，单用户、单活动任务；服务仅监听 `127.0.0.1`，不支持远程公开部署或多 worker。
- Shell 使用固定 Docker 权限，审批不能开放宿主资源、网络或特权模式；容器不构成对任意恶意代码的绝对隔离。每条命令结束即清理容器，不支持常驻服务。
- 文件工具默认处理不超过 64 KiB 的 UTF-8 文本；哈希和文件锁不能提供恶意外部修改下的完整事务保证。
- Web Redis 会话支持多次 Run 的近期上下文；SQLite 长期记忆、自动摘要、断点续跑、嵌套规范和多 Agent 尚未实现。
- 当前模型响应为非流式；轮数、时限及输入输出预算可能使任务提前结束。

输入最多 1000 个文件、合计 64 MiB；禁止链接及默认排除的凭据/环境目录。结束扫描最多 10000 项、深度 32、256 MiB、10 秒；不完整时不推断未确认的删除。中断任务不重放命令，服务启动时只清理归属记录与完整标签匹配的遗留容器，清理未确认时阻止新任务。

## 文档

- [学习与源码对照指南](docs/GEN1_LEARNING_GUIDE.md)：架构、源码入口、练习与自测。
- [交付与复现指南](docs/MVP_DELIVERY.md)：完整样例、运行方法和验收记录入口。
- [设计与验收文档](docs/)：详细设计、开发记录及验证证据。
