# 阶段三：简易 Web 与执行轨迹

阶段三沿用阶段一约定的 FastAPI，使用 Uvicorn 单进程单 worker、本地源码启动。前端为原生 HTML/CSS/JavaScript，不需要 Node 构建流程。默认地址 `http://127.0.0.1:8765`，不提供远程监听或账号功能。

## 运行与事件

`RunManager` 在服务的 asyncio 循环中持有一次运行的后台 Task、取消信号和模拟审批状态。先分配独立 RunStore，再注入既有 Runner；Runner 的 CLI 调用方式保持兼容。模型、文件工具、预算和取消沿用阶段二控制。页面断开、关闭或刷新均不改变后台 Task。一个服务进程只接受一个活动任务；第二次提交返回 409。没有任务自动重放、自动恢复或自动提交重试。

页面每 700 ms 轮询一次所选运行，历史列表每 3 秒更新。`events.jsonl` 是轨迹唯一来源；API 接受 after 序号，每页最多 50 条。前端按序号去重，只追加完整事件；刷新从序号零重建。超过 24000 字符的单条事件详情显示带标记的摘要，完整日志保留本地。切换运行时用独立代次丢弃旧请求的迟到响应。

运行标识保存于 URL hash 和浏览器 localStorage，仅用于选择已有记录；刷新不会 POST 新任务。模型回复、参数、结果、错误、重试和计划变更都在事件列表中展开查看。最新规则、计划、任务记忆和最终结果读取既有落盘快照。文件产物清单来自成功 write_file 事件的路径、字节数和 SHA256，不把模型在记忆中声称的产物当成写入证据。没有通用检查器时明确显示未验证，模型自检依据保留于轨迹和最终回复中。

历史运行没有终态文件且不属于当前活动 Task 时显示 interrupted，不会假定成功或重新执行。日志尾部不完整 JSON 行忽略；不尝试修复或续跑。后端磁盘记录可能含任务、资料和路径，应按本机文件权限管理。

## 取消与模拟审批

取消接口只设置运行器的取消信号并记录 cancel_requested。界面先显示 cancelling，等后端持久化终态才显示 cancelled。运行器在后续派发前检查信号；同步文件操作的取消限制沿用阶段二说明。

“模拟审批演练”创建独立的 approval_demo 运行，和模型任务共用单活动任务约束。它不构造模型、不执行 shell、不写工作目录；批准仅发出 simulated_action_completed，拒绝结束为未验证的拒绝结果。此演练不是模型失败后的兜底。

审批绑定 run_id、随机审批 id、固定模拟描述、工作目录和等待时限。等待最多 60 秒，同时受本次总时长预算约束；决定时重新校验时限，不能依赖等待协程恰好已经醒来。后端单循环内检查 pending 状态并一次性置位 Future，重复/错误绑定/终态/取消后决定返回 409。超时发出 approval_expired，取消发出 approval_cancelled。服务重启后旧审批失效，不从历史事件恢复授权。

这是阶段三的模拟状态机，尚未把审批接入真实模型工具或进程执行。阶段四仍需绑定实际 shell 参数、实现进程树控制并另行验收。

## 本地接口边界

- 启动器固定绑定 127.0.0.1，关闭反向代理头信任；不提供 CORS。
- 每个请求验证包含端口的精确 Host；有 Origin 时必须与服务同源，拒绝 cross-site/same-site Fetch Metadata。
- 每次服务启动生成随机会话 token，通过同源 `/api/session` 获取；任务数据接口须带 X-Harness-Token。POST 还必须有精确 Origin 和 application/json。
- 请求体在读取时限制为 128 KiB，读取时限 5 秒，校验 JSON 对象与允许字段。运行目录使用 32 位十六进制标识，不接收任意日志路径。
- 所有动态内容使用 textContent/文本节点，不解析 Markdown 或 HTML。响应设置 CSP、禁止嵌入、nosniff、no-store、no-referrer；无外部脚本或 CDN。
- 不在访问日志打印任务或 token；模型密钥沿用 RunStore 脱敏。模型配置缺失返回错误，不切换到演练。

这些措施面向无关网页触发本地接口的风险，不作为多用户鉴权、远程部署或操作系统沙箱。服务端口、进程及 runs 目录仍是本地可信资源；不支持多 worker 或多实例共享写入。

接口：GET `/api/session`、GET/POST `/api/runs`、GET `/api/runs/{id}`、GET `/api/runs/{id}/events?after=0`、POST `/api/runs/{id}/cancel`、POST `/api/runs/{id}/approvals/{approval_id}`。

实现参考 FastAPI 官方的 [生命周期](https://fastapi.tiangolo.com/advanced/events/) 与 [中间件](https://fastapi.tiangolo.com/advanced/middleware/)；项目实际参数与边界以本实现和测试为准。
