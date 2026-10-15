# 阶段三验收

日期：2026-10-15。环境：Windows，已有 conda `harness`，Python 3.12；FastAPI 0.143.1、Uvicorn 0.54.0、HTTPX 0.28.1。

## 自动检查

全套 51 项 unittest：原有 41 项，加 10 项 Web 测试。完整结果见 `stage-three-tests.txt`。

- 从 Web API 完成有计划、规则快照、读写文件、最终答案的任务；页面快照和事件与落盘内容一致。
- GET 刷新和增量重读不产生新模型调用，游标分页、截断标记和历史中断显示有效。
- 同一时刻第二次启动被拒绝；取消等待中的模型；同一模型响应包含两次工具调用时，第一次调用触发取消后第二次未派发。
- 审批批准仅一次有效；并发重复决定、错误绑定、取消后及过期批准被拒绝；拒绝不产生模拟完成事件。
- 断开 HTTP 客户端不取消后台运行；服务关闭逻辑取消运行并等待终态。
- Host、Origin、Fetch Metadata、token、内容类型、JSON 形状、未知模式和过大正文校验。
- CSP 等响应头、无 CORS、动态文本展示路径；模型配置缺失返回错误。

`node --check harness/static/app.js` 通过。`pip check` 仍检出现有环境 IPython 9.17.1 和 ipython-pygments-lexers 1.1.1 缺少 pygments，与阶段二一致，本次未修改这两项无关依赖。

## 真实浏览器和模型

通过 Codex 内置浏览器打开本机服务，填写独立样例工作目录并点击“开始模型任务”。运行 `ada2b6906ded452ca6d36b2d86dca093` 使用 qwen3.8-flash，生成 summary.txt，总人数为 42，包含“来源：input.txt”，为 UTF-8 无 BOM、LF。

实际工作目录：`runs/stage-three-workspace-6c00934007c447fcbee8dc0051558ae3/`。运行目录：`runs/ada2b6906ded452ca6d36b2d86dca093/`。终态 unverified，三步计划全部 completed，共 106 条事件。浏览器展示最终答案、规范哈希、计划、产物路径与哈希；刷新后仍为同一个运行，106 条事件对应 106 个轨迹节点，没有重复。

初次浏览器测试发现前端函数名覆盖 History API，已修复为 loadHistory；刷新后原后台任务继续运行并恢复展示，没有重新提交。最终布局截图见 `stage-three-workbench.jpg`。

独立 HTTP 复核读取同一次真实运行（未再次调用模型），8 项检查全部通过：终态标识、样例规则、连续序号、页面/日志对应、快照对应、产物哈希、刷新无执行、计划完成。配置及证据见 `stage-three-live.json`。

真实浏览器另外操作了模拟审批：

| 流程 | 运行标识 | 结果 |
| --- | --- | --- |
| 批准 | 701e720b3112419aad9fe4202946f37c | 记录一次模拟完成，不执行真实命令 |
| 拒绝 | bd66b430f8414b3c9591961cd05ada51 | 显示“模拟操作已拒绝” |
| 取消 | febbed606eaa4a1aa792430b53e1b73c | 显示 cancelled，审批无效 |

安全展示夹具 `e0ea950d7be24c398a075b83f981cedd` 含 `<script>` 和 `<img onerror>` 字符串。浏览器展开工具反馈后完整显示这些字符串；DOM 检查轨迹内 script/img 节点数为 0，注入标记不存在。夹具为显式测试记录，不是模拟模型 provider。

## 复现

```powershell
conda activate harness
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
node --check harness/static/app.js
python -m harness serve --env-file .env --config config.example.json
```

打开 `http://127.0.0.1:8765`。模型配置由现有 `.env` 提供；长任务可在自用 JSON 配置中调整轮数、completion tokens 和请求/总时限。本次真实验收采用 20 轮、4096 completion tokens、60 秒单次请求和 360 秒总时限，未改变应用默认值。

已有落盘运行可复核：`python -m tests.live_stage_three --run-id ada2b6906ded452ca6d36b2d86dca093`。不带 run-id 时脚本会复制样例并通过 Web API 启动新的真实模型任务。

交付前检查所有本次创建/修改的文本（含配置、测试输出及运行日志）编码/换行和 `git diff --check`。未修改全局 Git 配置和暂存区。阶段四 shell 与进程控制尚未接入，通用结果验证器仍属后续阶段。
