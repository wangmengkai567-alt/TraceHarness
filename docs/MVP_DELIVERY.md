# AI Agent Harness MVP 交付与复现

交付日期：2026-10-16。阶段一至六完成；本地单用户、单任务、Python/FastAPI 单体、源码运行。完整证据见 [阶段六验收](STAGE_SIX_ACCEPTANCE.md)。

## 从源码启动

在仓库根目录使用已初始化 conda 的 PowerShell：

```powershell
conda env create -f environment.yml
conda activate harness
if (-not (Test-Path -LiteralPath .env)) { Copy-Item -LiteralPath .env.example -Destination .env }
# 在本机填写 HARNESS_BASE_URL、HARNESS_MODEL、HARNESS_API_KEY。
python -m harness serve --env-file .env --config config.example.json
```

已有环境时用 `python -m pip install -r requirements.txt` 更新依赖，不必重建；项目不通过 pip 安装，不使用 venv。复制 .env 的步骤只在首次配置时执行，已有 .env 请保留。程序不会自动发现父目录密钥，也不会自动回退到模拟模型。

打开输出的精确地址（默认 `http://127.0.0.1:8765`），填写已有工作目录和任务。修改 JSON/环境配置后重启服务；新任务保存自己的配置快照。服务只绑定回环地址，不支持账号、远程公开部署、多 worker 或多实例共享执行。

## 在真实页面手工完成样例

先复制样例到独立目录，避免污染仓库模板：

```powershell
conda activate harness
New-Item -ItemType Directory -Path runs -Force
$sampleDir = Join-Path (Get-Location) ("runs/stage-six-manual-" + [guid]::NewGuid().ToString("N"))
Copy-Item -LiteralPath examples/stage_six -Destination $sampleDir -Recurse
Write-Output $sampleDir
Get-Content -LiteralPath (Join-Path $sampleDir task.txt) -Raw
python -m harness serve --env-file .env --config (Join-Path $sampleDir config.json)
```

将输出的 sampleDir 和 task.txt 内容填入 Web。预期：加载 AGENTS.md → 建立计划 → 读取材料 → 记录有来源事实 → 写 summary.txt → 第一次审批 `python verify.py` → 模型消费结果并收尾 → 第二次独立审批同一检查命令 → “指定检查通过”。两次审批均显示固定工作目录、实际 shell/argv 和 15 秒时限；批准前核对参数。

生成的 summary.txt 必须是 UTF-8 无 BOM、LF，且精确四行并保留末尾换行：

```text
资料摘要
合计：105
乘三结果：315
来源：input.txt
```

规范、材料和 verify.py 应保持不变。样例沿用默认四轮近期交互；复杂任务可以调整轮数和上下文，但不能将更大的预算当作成功保证。记忆和计划始终进入后续模型输入，历史日志本身不等于记忆。

## 自动复现与原始记录

```powershell
python -m unittest discover -s tests -v
node tests/test_web_ui.js
python -m pip check
# 会消耗实际模型用量，验收客户端只批准固定无害命令：
python -m tests.live_stage_six
```

真实模型验收脚本复制样例并启动独立临时端口上的服务，经正常 HTTP API 提交任务，核对并批准最多两次预设命令，核对产物和事件，最后停止服务、等待进程退出并确认监听端口释放。失败也执行清理。它是测试夹具，不向生产 Web 增加自动批准、模拟 provider 或调试执行接口。

`python -m tests.live_stage_six --browser` 则等待人在真实页面提交任务与点击审批；输出 manifest、目录和地址。页面完成后，在另一个已激活环境的终端创建结束标记，脚本再检查并关闭本次服务：

```powershell
$manifest = Get-Content -LiteralPath runs/stage-six-current.json -Raw | ConvertFrom-Json
New-Item -ItemType File -Path (Join-Path $manifest.root finish)
```

此模式最长等待 650 秒，任务本身使用配置的 480 秒预算和 120 秒审批等待。终端异常退出时仍应核对本次 manifest 的服务进程与端口；不要按端口批量终止其他服务。

每次真实样例的目录与编号见 docs/stage-six-live.json：

- workspace：规范、材料、检查脚本和产物。
- run_directory：metadata.json、rules.json、state.json、events.jsonl、result.json。
- server_cleanup：本次服务 PID、退出码、端口释放证据。

```powershell
# records 路径从实际报告的 run_directory 父目录取得。
python -m harness inspect <run-id> --runs-dir <records目录> --events
```

同一 runs_dir 内编号唯一；不同隔离验收目录可能有相同日期编号，复核时须同时使用 records 路径。docs 内报告与截图可随源码交付；runs 是本机原始记录，不纳入版本控制，清理前应自行备份。

## 结束、取消与失败

页面关闭不取消任务；请点“取消运行”。审批未决定时关闭页面不会执行命令，等待会过期。刷新只恢复轨迹，不重跑任务。停止日常服务使用 Ctrl+C，并等待正在执行的命令树清理结束。

| 状态 | 用户应如何理解 |
| --- | --- |
| completed | 仅配置的检查项已通过 |
| unverified | 模型结束，没有配置检查，仍需人工复核 |
| verification_failed | 配置检查未通过，允许的修正次数已用完 |
| failed | 请求或执行器失败，查看 reason 和操作证据 |
| cancelled | 任务已取消；已发生的副作用不会撤销 |
| budget_exceeded | 原任务预算已耗尽，不会另开预算继续 |
| interrupted | 没有终态的旧运行，不续跑、不复用批准 |

重复副作用不盲目重放：不确定写入先读取状态；相同不确定 shell 命令普通重调被阻止；拒绝的相同命令不反复请求批准。配置的可重复检查每次仍需新审批。进程终止未确认时为 failed，当前服务阻止新任务，须人工检查后重启。

## 明确限制

Shell 审批不是沙箱，批准的命令能访问工作目录外资源、网络与继承环境，仅用于可信项目。文件工具的路径边界不约束 shell。Windows Job Object 管理正常子孙进程，不覆盖借助系统服务等外部代理启动的独立进程。POSIX 分支本次未验收。

仅支持有限文本文件操作、两类配置检查和 OpenAI 兼容 Chat Completions 接口；没有搜索工具、跨任务记忆、断点续跑、通用 hooks、多 Agent、模型路由、账号、自动提交/部署或项目隔离。任务记忆、文件哈希与日志不能替代沙箱或事务。下一迭代在开放自主 shell 前必须先具备并验收隔离能力。
