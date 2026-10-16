"""Check exact bytes without editing any sample file."""
from pathlib import Path
import sys


def main():
    expected = "资料摘要\n合计：105\n乘三结果：315\n来源：input.txt\n".encode("utf-8")
    path = Path(__file__).with_name("summary.txt")
    actual = path.read_bytes() if path.is_file() else None
    passed = actual == expected
    print("CHECK PASS: summary bytes, values, source, UTF-8 and LF" if passed else
          "CHECK FAIL: summary.txt must match the four required UTF-8/LF lines")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
