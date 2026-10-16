# 阶段四：Shell 审批与进程控制

日期：2026-10-15

沿用 Python/FastAPI 单体、同源 API 与轮询页面；不改变部署架构。

## 能力与审批

Web 模型运行提供 `run_command(command, timeout_seconds)`。工作目录固定为本次运行目录，Windows 固定使用系统 Windows PowerShell（NoLogo/NoProfile/NonInteractive），POSIX 使用 `/bin/sh -c`。模型不能指定其他 shell、目录或环境变量集合。普通 CLI 未连接审批代理，因此不提供 shell。

命令非空、无 NUL、最多 8000 字符；时限必须为正有限数，且不超过配置 `shell_timeout_seconds`（默认 60）。额外参数拒绝。参数验证通过后生成审批，显示 run_id、call_id、独立审批 ID、命令、shell、完整 argv、工作目录、执行时限及审批时限。执行使用同一个局部不可变 argv 元组，审批接口仅接受 decision，不接受替换参数。

审批必须通过同源 token 保护的 POST 明确批准。拒绝、过期作为工具错误反馈模型；每次重新调用都生成新审批。重复决定、错误审批 ID、取消后、过期后、终态后的批准返回 409，不执行。批准只消费一次。等待审批计入总时长，但不占普通文件工具的 5 秒时限；默认审批等待 60 秒，可通过 `approval_timeout_seconds` 调整。

页面断开时不会自动批准；待审批调用在后端过期。已经通过 POST 批准的调用由后端执行，页面是否继续在线不影响其时限。刷新只读取快照与事件，不启动或批准命令。审批演练继续保留并标记为 simulation。

## 进程生命周期

Windows 使用 CREATE_SUSPENDED / CREATE_NO_WINDOW 创建进程，先通过 AssignProcessToJobObject 纳入本次独立的 Job Object，再用 ResumeThread 恢复执行。Job 设置 KILL_ON_JOB_CLOSE；正常创建的子孙进程自动受 Job 管理，不开启 breakaway。创建、赋予 Job 或恢复失败时，关闭执行路径并清理已创建进程。依据微软 [Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects) 与 [AssignProcessToJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject) 文档实现。

命令超时、用户取消、任务总时长耗尽、服务正常关闭都必须先终止 Job，查询 ActiveProcesses 为零，并等待根进程和输出管道结束，才记录 `shell_process_stopped`。正常 shell 退出时同样清理剩余后台子进程。创建进程的 await 被 shield，避免取消竞态丢失已创建的进程句柄。stdin 关闭，不支持交互式输入。

如果清理失败或 5 秒内不能确认进程树结束，运行记为 failed / process_termination_unconfirmed，不能记为 cancelled；停止后续工具派发，当前服务拒绝新任务。须人工检查残留进程后重启服务。本阶段不实现自动恢复或重放。总预算终止后允许额外的有限清理时间。

POSIX 使用独立 session/process group、SIGKILL 与进程组消失确认。本次验收在 Windows 执行，未把 POSIX 分支宣称为已验收。

## 输出与边界

合并 stdout/stderr，持续排空管道，仅保留有上限的字节前缀。结果包含 exit_code、output、output_bytes、truncated、timed_out；失败为 shell_command_failed，超时为 shell_timeout。UTF-8 解码使用替换策略，非 UTF-8 程序输出可能有替换字符。JSON 转义后再次裁剪输出，通常保留退出码等结构化字段；极低 max_output_chars 仍可能触发全局输出截断。

exit_code 为实际 shell 退出码。Windows PowerShell 的原生命令失败可能表现为 shell 退出码 1；如需传递原生退出码，命令应显式 `exit $LASTEXITCODE`，此内容也须展示并获批。超时发生时 exit_code 可为 null，不能当作正常退出。

本功能不是沙箱，仅用于可信本地项目。批准的命令能访问目录外资源、网络及继承的环境；文件工具的目录约束不约束 shell。强制结束前发生的副作用不会撤销。Job 管理正常后代，不覆盖通过系统服务等外部代理创建的独立进程。强杀宿主的可靠恢复、通用检查器及完整失败修正属于后续范围。
