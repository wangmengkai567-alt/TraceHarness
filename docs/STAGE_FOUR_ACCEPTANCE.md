# 阶段四验收

日期：2026-10-15。环境：Windows，已有 `harness` conda 环境，Python 3.12；未使用 venv。

## 自动检查

78 项 Python 测试全部通过，Node 前端 DOM 回归通过，`pip check` 最终通过。依赖检查最初发现现有 IPython 缺少 Pygments，已在 harness conda 环境补齐，未修改项目依赖清单。全部 73 个受管理及未忽略的 UTF-8 文本文件扫描无换行不合规，本次创建与修改文件均为 UTF-8 无 BOM、LF，`git diff --check` 通过。

执行记录见 [stage-four-tests.txt](stage-four-tests.txt)。新增阶段四测试覆盖：

- 未批准等待、拒绝、取消、过期、终态后的调用不执行；拒绝反馈进入后续模型输入。
- 审批显示参数与实际执行参数一致，接口拒绝替换参数；重复批准只执行一次；下一调用需独立审批。
- 参数非法不进入审批；退出码、失败反馈、百万字节输出的有界排空与截断。
- Windows 上命令正常创建 Python 父子进程，子进程监听独立临时端口；分别触发 shell 超时、用户取消、任务总时长耗尽与服务关闭，检查父子 PID 均退出且端口能重新绑定。
- 终止未确认时为 failed，不能误报取消；后续命令和新任务均被阻止，包括取消清理过程中失败的路径。
- DOM 测试覆盖 shell 审批参数按文本安全显示、真实执行按钮文案、已决定审批按钮禁用。

## 真实模型 + Web API

[stage-four-live.json](stage-four-live.json) 记录模型、配置、时间、实际审批参数及六项通过证据。运行 ID 为 `20261015004`。

`python -m tests.live_stage_four` 启动进程内 FastAPI 应用，通过 HTTP ASGI 客户端提交真实模型任务。模型请求 shell 后，验收客户端核对预先准备的唯一无害命令、目录与时限，再通过正常审批 POST 批准；未知命令拒绝验收并取消任务。此客户端仅用于固定验收，不是产品的自动审批路径。

模型经审批启动系统 PowerShell，执行 `check.py`，写出 UTF-8 无 BOM、LF 的 `proof.txt`，输出 `stage-four: 315`。检查真实 shell 退出码 0、产物精确字节、最终答案使用 315、进程树清理事件和 unverified 终态，六项全部通过。完整事件和产物保留在本地 runs 目录。

此次未启动持久后台 Web 服务；ASGI 应用测试结束调用 manager.shutdown。临时子进程的 PID 和监听端口释放已由测试断言确认。未影响用户已有服务。

## 范围限制

真实模型验收证明模型、Web 审批 API、shell、输出与最终答复闭环；浏览器交互本次使用 DOM 回归测试，未将其写为真实浏览器点击验收。Windows 进程树执行器检查使用确定性模型，避免把安全边界依赖于模型选择。没有通用结果验证器，模型完成仍为 unverified；阶段五、六未自动开始。其他边界见 [阶段四设计](STAGE_FOUR_DESIGN.md)。
