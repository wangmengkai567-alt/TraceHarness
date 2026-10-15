# AI Agent Harness MVP — 阶段三

已实现 Python 的最小运行核心：任务 → 模型 → 受控工具 → 反馈 → 终态。模型使用 OpenAI 兼容的 Chat Completions 工具协议，模型及服务地址由用户配置。

包含配置、有限请求重试、运行预算、取消和事件记录，以及根目录 `AGENTS.md` 冻结快照、可调整计划、带来源的任务记忆、受限上下文和工作目录内的文本文件工具。已加入本地 Web 任务工作台、执行轨迹、取消和模拟审批；尚无真实 shell 或 Tavily 工具。

阶段三使用方法与验收见 [阶段三验收](docs/STAGE_THREE_ACCEPTANCE.md)，架构与接口边界见 [阶段三设计](docs/STAGE_THREE_DESIGN.md)。阶段二记录保留于 [阶段二验收](docs/STAGE_TWO_ACCEPTANCE.md)。

## 启动

本项目使用 **conda** 管理环境，环境名称为 `harness`，Python 3.12。克隆仓库后，先进入仓库根目录，再在已初始化 conda 的 PowerShell 或 Anaconda Prompt 中运行：

环境使用 conda 加源码直接运行；本次检查在已有 harness conda 环境执行。程序仅使用真实 LLM，没有 demo 模型或离线兜底。

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

阶段一工具为 `add_integers` 和 `multiply_integers`，执行真实整数运算，校验参数且不使用 eval。它们继续用于验证真实 LLM → 工具 → 结果反馈 → 后续调用的闭环。阶段二新增文件工具；尚无联网工具。

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

`run` 必须显式提供 `--task`，没有预置业务任务或模拟 provider。文件任务使用 `--workspace <目录>` 指定边界；联网搜索和 shell 尚未接入。

配置优先级：命令行覆盖项 > 现有进程环境 > 显式 `.env` > JSON 配置 > 默认值。不会自动寻找父目录 `.env`；需传入 `--env-file`。JSON 使用 `config.example.json` 中的字段，未知字段（包括密钥字段）会被拒绝。

默认 `completion_token_parameter` 为 `max_tokens`。若服务要求 `max_completion_tokens`，在 JSON 中切换此字段；参数数值由 `max_completion_tokens` 配置项控制。HTTP 400 不会自动反复试不同参数。

默认单次模型请求时限为 90 秒，任务总时限为 360 秒，最多重试 2 次。当前使用非流式模型响应，单次时限覆盖等待完整答案的全过程，总时限还包含工具、重试等待及后续交互。自定义 JSON 中的时限优先于默认值；修改配置后需重启服务，新任务才会采用新值，已有任务记录保留原始配置。页面“配置与运行说明”显示所选任务实际使用的单次请求和总时限。

## 轨迹与取消

每次运行生成 `runs/<run-id>/`：

新任务 ID 使用服务所在电脑的本地日期加当日序号，例如 `20261015001`、`20261015002`，次日从 `20261016001` 开始。序号至少三位，超过 999 后继续为 1000。CLI 和 Web 共用同一规则，在同一个 `runs_dir` 内根据当天已有目录的最大序号继续分配，重启不重置；并发创建通过原子目录创建避免重号。失败或中断任务同样占用编号，请保留运行目录以维持编号连续性。既有 UUID 任务仍可查看，无需迁移。

- `metadata.json`：任务、工作目录、配置快照、运行类型；不包含密钥。
- `events.jsonl`：连续序号的请求、响应、工具反馈、错误和重试事件。
- `result.json`：最终状态、答案、计划/记忆、用量（若模型提供）与耗时。
- `rules.json`：根目录规范内容与原始字节 SHA256；下次运行才加载修改。
- `state.json`：最新计划和任务内记忆；事件包含调整原因和历史。

```powershell
python -m harness inspect <run-id> --events
```

自定义了 `runs_dir` 时，查看命令传入 `--runs-dir <目录>`。运行目录相对于启动时的当前目录解析。

运行中按 Ctrl+C 发出取消信号：停止后续操作，取消等待中的请求/退避，并尽量保存取消终态。嵌入应用可向 `Runner.run` 传入 `asyncio.Event`。远端模型是否已消耗用量，不因客户端取消而得到保证。

正常模型结束为 `unverified`，因为尚未配置通用结果验证器。其他终态为 `failed`、`cancelled`、`budget_exceeded`。无终态文件时查看器报告 `no_terminal_result`：可能仍在运行，也可能意外中断，不会误报完成或自动续跑。

退出码：正常结束 0、执行失败/预算耗尽/闭环验收失败 1、配置或文件错误 2、用户取消 130。

## 检查

```powershell
conda activate harness
python -m unittest discover -s tests -v
python -m pip check
```

测试使用标准库 `unittest`。HTTP 集成测试使用真实本地 socket，但服务返回模拟响应，未连接真实 LLM。

参考：[分阶段计划](HARNESS_MVP_IMPLEMENTATION_PLAN.md)、[阶段一设计](docs/STAGE_ONE_DESIGN.md)、[阶段一验收记录](docs/STAGE_ONE_ACCEPTANCE.md)。

## 阶段二文件任务

先准备独立工作目录，放入材料及可选的根目录 `AGENTS.md`。模型先用 `update_plan` 建立步骤，再调用 `list_files`、`read_file`、`write_file`，通过 `update_memory` 保留带来源的事实、推测和进度。

```powershell
python -m harness run --env-file .env --workspace examples/stage_two --task "读取 input.txt，记录发现并按 AGENTS.md 创建 summary.txt，读取产物核对并更新计划。"
# 完整真实模型验收，复制样例到 runs 中，保留检查记录：
python -m tests.live_stage_two
```

现有文件写入须提供读取时返回的 `sha256` 作为 `expected_hash`；新文件使用 null。文件默认最多 64 KiB，规则最多 16 KiB，目录最多 200 项；完整读取超限拒绝，工具输出过长明确标记 truncated。已有 UTF-8 BOM 与 LF/CRLF 保留，新文件 UTF-8 无 BOM、LF。更多编码、混合换行和大文件暂不支持。

`config.example.json` 新增 `max_file_bytes`、`max_rules_bytes`、`max_directory_entries`、`recent_history_turns`、`max_state_chars`。默认保留最近 4 轮完整交互；规范、任务、计划和记忆始终进入输入，超过总输入预算明确结束。复杂任务可适当提高轮数和 completion token 上限；真实验收脚本使用 20 轮、4096 completion tokens。

文件工具限制面向可信本地目录，不能替代沙箱；已有文件写入使用哈希冲突检测和文件锁，不能保证恶意外部修改下的无竞态事务。记录持久化不代表支持续跑。详细限制见阶段二设计文档。

## 阶段三 Web 工作台

激活 conda 环境、安装新增依赖并填写本机 `.env` 后启动：

```powershell
conda activate harness
python -m pip install -r requirements.txt
python -m harness serve --env-file .env --config config.example.json
# 可用 --port 8766 更换端口
```

打开终端给出的 `http://127.0.0.1:8765`，填写已有工作目录及任务，点击“开始运行任务”。页面展示状态、规范快照、计划/调整原因、记忆、工具参数/反馈、模型回复、错误/重试、最终答案、产物与检查说明。长详情可展开，超限明确截断；日志与原始 HTML 始终按文本安全展示。

当前 Harness Studio 界面采用任务配置、标签式结果区和右侧运行信息三栏布局。运行日志在独立的深色窗口内滚动，可暂停跟随、清空显示和查看详情；页面最多保留最近 300 条日志/轨迹节点，完整记录保存在本地。回答支持安全的基础 Markdown 排版。界面验证及截图见 [界面优化记录](docs/STUDIO_UI_REDESIGN.md)。

“模拟审批演练”独立测试批准、拒绝、取消与时限，不调用模型，不执行真实命令或写工作目录。一次只能运行一个模型任务或演练。审批默认等待 60 秒，也受总时限约束；重复或失效决定由后端拒绝。

刷新或重开页面可恢复所选轨迹，不会重复启动任务；历史运行可用下拉框切换。关闭页面不等于取消，需使用“取消运行”；界面先显示“正在取消”，等后端终态落盘后才显示“已取消”。停止服务用 Ctrl+C，后端通知活动任务取消。服务重启后请刷新页面获取新会话；没有终态的旧记录显示中断，不自动续跑或恢复审批。

服务只绑定 127.0.0.1，请使用终端提供的精确地址（localhost 别名会被 Host 校验拒绝）。接口要求同源请求与会话 token，未开放 CORS、远程部署、账户或多 worker。运行时限和模型仍通过环境变量/JSON 配置，Web 不提供模型配置后台。

```powershell
# 服务运行时，通过 API 启动新的真实模型样例任务并验收：
python -m tests.live_stage_three
# 只复核已有记录，不发起新的模型调用：
python -m tests.live_stage_three --run-id <run-id>
```

全套检查为 51 项，真实 Web 模型样例及浏览器交互已验证，详细证据见阶段三验收。正常模型结束仍为 unverified；模型自检与计划 completed 不表示通用检查器已通过。真实 shell、进程树控制及完整失败恢复继续按阶段四、五推进。

## 问答与轮数预算

通用知识问答可直接回答，不要求先查工作目录。模型每轮会收到剩余轮数提示；`max_turns` 的最后一轮专用于最终答复，不执行工具，不增加预算外的请求。默认 8 轮因此包含最多 7 轮工具交互和 1 轮收尾；配置为 1 轮时只能直接回答。重试仍计入 `max_model_calls`，总时长与取消继续生效。

页面显示轮数上限和可读停止原因。`turn_limit` 表示达到交互轮数上限且未收到有效最终答复，计划标记 completed 或记忆声称“已回答”不能替代答案；历史失败不会自动重跑或变为成功。修复记录见 [问答轮数耗尽修复](docs/TURN_LIMIT_FIX.md)。已启动的服务需重启后加载后端修改。
