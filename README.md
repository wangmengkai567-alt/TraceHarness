# AI Agent Harness MVP — 阶段一

已实现 Python 的最小运行核心：任务 → 模型 → 受控工具 → 反馈 → 终态。模型使用 OpenAI 兼容的 Chat Completions 工具协议，模型及服务地址由用户配置。

包含配置、有限请求重试、轮数/调用数/总时长/请求超时/输入输出限制、取消、结构化工具结果和本地事件记录。阶段一不包含 Web、`AGENTS.md` 加载、规划/任务记忆、文件工具、shell 或 Tavily 工具。

工程检查与模拟 HTTP 验收已通过；真实模型验收等待本地接口和密钥配置，两种验收不能相互替代。

## 启动

本项目使用 **conda** 管理环境，环境名称为 `harness`，Python 3.12。克隆仓库后，先进入仓库根目录，再在已初始化 conda 的 PowerShell 或 Anaconda Prompt 中运行：

环境由你自行创建与激活。2026-10-15 已改为 conda 加源码直接运行；35 项执行器检查已在已有 Python 环境复验，尚未在新 conda 环境复验。程序仅使用真实 LLM，没有 demo 模型或离线兜底。

```powershell
conda env create -f environment.yml
conda activate harness
# 接下来填写本机 .env，按下节命令运行真实模型。
```

`environment.yml` 创建 conda 环境，并通过该环境内的 pip 读取 `requirements.txt`、安装运行依赖。

若环境已创建，无需重复创建；激活后可更新运行依赖：

```powershell
conda activate harness
python -m pip install -r requirements.txt
```

未激活环境时，也可明确指定 conda 环境运行：

```powershell
conda run -n harness python -m harness run --env-file .env --task "使用工具计算 47+58，再把结果乘以3。"
```

阶段一工具为 `add_integers` 和 `multiply_integers`，执行真实整数运算，校验参数且不使用 eval。它们用于验证真实 LLM → 工具 → 结果反馈 → 后续调用的闭环，不代表已具备文件操作或联网能力。

## 配置真实模型

复制 `.env.example` 为本机 `.env`:

cp .env.example .env

填写：

- `HARNESS_PROVIDER=openai_compatible`
- `HARNESS_BASE_URL`：所选服务的 API 基础地址，不包含 `/chat/completions`。程序不预设供应商地址，不替你选择区域或服务。
- `HARNESS_MODEL`：服务实际接受的模型标识。样例保留你提出的 `Qwen-3.8-flash`，其可用性及准确 API 标识尚未验证。
- `HARNESS_API_KEY`：本地密钥，不发送到聊天，不写入 JSON 配置。
- `TAVILY_API_KEY`：为后续搜索工具预留；阶段一不会使用。

执行真实工具闭环验收：

```powershell
python -m harness live-check --env-file .env --config config.example.json
```

`live-check` 调用真实接口，要求模型先使用工具计算 47+58，再把返回值乘以3，消费工具反馈后回答315。成功输出 `live_check_passed: true`；这验证工具闭环，不代表通用任务质量。缺少配置会报错，不会降级为模拟。

一般任务入口：

```powershell
python -m harness run --env-file .env --task "先用工具计算47+58，再用返回值乘以3，报告结果。"
```

`run` 必须显式提供 `--task`，没有预置业务任务或模拟 provider。阶段一不能完成需要文件、联网搜索或 shell 的任务。

配置优先级：命令行覆盖项 > 现有进程环境 > 显式 `.env` > JSON 配置 > 默认值。不会自动寻找父目录 `.env`；需传入 `--env-file`。JSON 使用 `config.example.json` 中的字段，未知字段（包括密钥字段）会被拒绝。

默认 `completion_token_parameter` 为 `max_tokens`。若服务要求 `max_completion_tokens`，在 JSON 中切换此字段；参数数值由 `max_completion_tokens` 配置项控制。HTTP 400 不会自动反复试不同参数。

## 轨迹与取消

每次运行生成 `runs/<run-id>/`：

- `metadata.json`：任务、工作目录、配置快照、运行类型；不包含密钥。
- `events.jsonl`：连续序号的请求、响应、工具反馈、错误和重试事件。
- `result.json`：最终状态、答案、用量（若模型提供）与耗时。

```powershell
python -m harness inspect <run-id> --events
```

自定义了 `runs_dir` 时，查看命令传入 `--runs-dir <目录>`。运行目录相对于启动时的当前目录解析。

运行中按 Ctrl+C 发出取消信号：停止后续操作，取消等待中的请求/退避，并尽量保存取消终态。嵌入应用可向 `Runner.run` 传入 `asyncio.Event`。远端模型是否已消耗用量，不因客户端取消而得到保证。

正常模型结束为 `unverified`，因为阶段一尚未配置通用结果验证器。其他终态为 `failed`、`cancelled`、`budget_exceeded`。无终态文件时查看器报告 `no_terminal_result`：可能仍在运行，也可能意外中断，不会误报完成或自动续跑。

退出码：正常结束 0、执行失败/预算耗尽/闭环验收失败 1、配置或文件错误 2、用户取消 130。

## 检查

```powershell
conda activate harness
python -m unittest discover -s tests -v
python -m pip check
```

测试使用标准库 `unittest`。HTTP 集成测试使用真实本地 socket，但服务返回模拟响应，未连接真实 LLM。

参考：[分阶段计划](HARNESS_MVP_IMPLEMENTATION_PLAN.md)、[阶段一设计](docs/STAGE_ONE_DESIGN.md)、[阶段一验收记录](docs/STAGE_ONE_ACCEPTANCE.md)。
