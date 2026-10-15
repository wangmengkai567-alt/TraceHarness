# 阶段二验收

日期：2026-10-15。环境：Windows，`D:/Anaconda/envs/harness/python.exe`（已有 conda harness 环境）。

确定性测试覆盖：

- 原有模型适配、HTTP、重试、取消与预算检查。
- 新文件独占创建、已有文件哈希冲突、UTF-8 BOM / CRLF 保留、新文件 LF。
- 目录穿越、绝对路径、盘符相对路径、ADS、设备名及越界 Windows 目录联接。
- 硬链接拒绝写入；AGENTS.md 只读；过大文件、二进制、不支持编码拒绝；目录输出截断。
- 无规则运行、超大规则失败、冻结规则进入每轮输入、规则更新下次生效。
- 计划前置条件、非法状态校验、调整事件、记忆来源和 fact / hypothesis、历史过期后记忆保留。
- 固定输入超预算时未调用模型；运行快照与事件使用 LF。

41 项测试全部通过（35 项原有测试 + 6 项阶段二测试），最新一轮耗时 12.824 秒；完整记录见 `stage-two-tests.txt`。执行：

```powershell
conda activate harness
python -m unittest discover -s tests -v
python -m pip check
```

`pip check` 检出已有环境中 ipython 9.17.1 和 ipython-pygments-lexers 1.1.1 缺少 pygments；本项目依赖及 41 项测试可运行。本次未修改这些与 harness 无关的环境依赖。

真实模型验收使用本地 `.env`，不打印密钥。脚本在 `runs/stage-two-workspace-<id>` 复制样例，保留产物，并记录模型、运行标识、检查结果至 `stage-two-live.json`：

```powershell
python -m tests.live_stage_two
```

要求真实模型建立计划、读取输入、记录带来源的总人数事实、在后续请求使用记忆、产出符合规范的 summary.txt、读取核对并更新计划。此脚本的样例检查不改变核心 unverified 终态，也不等同于通用正确性验证器。

首次真实模型运行 `f084cb2f0ff84c92864b8e25952393e7`（qwen3.8-flash）已产出符合规范的摘要，记录计划/记忆并读取核对；收尾请求在 30 秒时限下连续超时，正确终止为 failed / model_request_timeout，未误报完成。记录见 `stage-two-live-first-attempt.json`。验收脚本随后使用 60 秒请求超时、360 秒总时限，并要求简洁计划/记忆；最新结果见 `stage-two-live.json`。应用默认时限未更改。

第二次运行 `40404b0231f1402b83b668c6135554c3` 于北京时间 2026-10-15 11:19:09 完成，六项验收检查全部通过：正常 unverified 终态、摘要规范、计划调整记录、全部计划步骤完成、记忆进入后续请求、读回产物哈希与实际文件一致。产物位于 `runs/stage-two-workspace-ee9ab15e88174bda9e4c744f26d4faa5/summary.txt`。检查器在该运行结束后读取同一份落盘事件和产物复核，没有再次调用模型或重放工具。

交付前扫描 Git 受管理及未忽略文本文件，均为 UTF-8 无 BOM、LF；`git diff --check` 通过。新建运行日志显式使用 UTF-8、LF。未改动全局 Git 配置或暂存区。
