"""本地单智能体任务执行框架。

阅读顺序：cli/web（入口）→ runner（执行循环）→ context（上下文与工具分发）
→ files/shell/search（实际操作）；store 保存日志，verification 判定配置检查结果。
static 目录是由同一个 Python 后端提供的网页界面。
"""
