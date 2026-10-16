"""Record full regression evidence with explicit UTF-8/LF output on Windows."""
import argparse
import io
import json
import logging
from pathlib import Path
import subprocess
import sys
import unittest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", default="node")
    args = parser.parse_args()
    logging.getLogger("asyncio").setLevel(logging.ERROR)
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(unittest.defaultTestLoader.discover("tests"))
    checks = {"python": {"tests": result.testsRun, "failures": len(result.failures),
                         "errors": len(result.errors), "passed": result.wasSuccessful()}}
    for name, command in [("web_ui", [args.node, "tests/test_web_ui.js"]),
                          ("dependencies", [sys.executable, "-m", "pip", "check"]),
                          ("diff", ["git", "diff", "--check"])]:
        completed = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace")
        checks[name] = {"exit_code": completed.returncode, "passed": completed.returncode == 0}
        stream.write("\n" + name + ":\n" + completed.stdout + completed.stderr)
    checks["passed"] = all(item["passed"] for item in checks.values())
    Path("docs/stage-seven-tests.txt").write_text(stream.getvalue().replace("\r\n", "\n"), encoding="utf-8", newline="\n")
    Path("docs/stage-seven-checks.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2) + "\n",
                                                 encoding="utf-8", newline="\n")
    print(json.dumps(checks))
    return 0 if checks["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
