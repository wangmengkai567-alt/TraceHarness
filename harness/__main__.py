# 执行 python -m harness 时从这里进入；具体的子命令解析交给 cli.main。
from .cli import main

# 将 main 的返回值作为进程退出码，供终端和自动化脚本判断是否成功。
raise SystemExit(main())
