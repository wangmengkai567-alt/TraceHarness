"""Create local Redis credentials without printing or replacing existing secrets."""
import hashlib
import secrets
from pathlib import Path

from dotenv import dotenv_values


def main():
    root = Path(__file__).resolve().parents[1]
    folder = root / ".redis"
    env = root / ".env"
    raw = env.read_bytes() if env.exists() else b""
    password = dotenv_values(env).get("HARNESS_REDIS_PASSWORD") if env.exists() else None
    if (folder / "users.acl").exists():
        expected = hashlib.sha256((password or "").encode()).hexdigest()
        if not password or ("#" + expected) not in (folder / "users.acl").read_text(encoding="utf-8"):
            raise ValueError("existing Redis ACL and .env do not match; resolve explicitly")
        print("Existing Redis credentials preserved.")
        return
    folder.mkdir(exist_ok=True)
    if not password:
        password = secrets.token_hex(32)
        newline = b"\r\n" if b"\r\n" in raw else b"\n"
        lines = raw.splitlines(keepends=True)
        replacement = b"HARNESS_REDIS_PASSWORD=" + password.encode() + newline
        matched = False
        for i, line in enumerate(lines):
            if line.strip().startswith(b"HARNESS_REDIS_PASSWORD="):
                lines[i] = replacement
                matched = True
        if not matched:
            if raw and not raw.endswith((b"\n", b"\r")):
                lines.append(newline)
            lines.append(replacement)
        env.write_bytes(b"".join(lines))
    admin = secrets.token_hex(32)
    acl = ("user default off\nuser harness on #" + hashlib.sha256(password.encode()).hexdigest()
           + " ~harness:sessions:* +get +set +eval +ping +client|setinfo\nuser admin on #"
           + hashlib.sha256(admin.encode()).hexdigest() + " ~* +@all\n")
    (folder / "users.acl").write_text(acl, encoding="utf-8", newline="\n")
    (folder / "admin-password").write_text(admin + "\n", encoding="utf-8", newline="\n")
    print("Redis ACL created; application password stored in local .env. No credentials printed.")


if __name__ == "__main__":
    main()
