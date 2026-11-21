"""Reject comments that are not part of the existing ratchet baseline."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tokenize
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

_SOURCE_SUFFIXES = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".vue",
    ".css",
    ".scss",
    ".html",
    ".sh",
    ".yml",
    ".yaml",
    ".toml",
    ".conf",
    ".sql",
    ".ini",
    ".cfg",
    ".properties",
}
_BASELINE = Path("scripts/comment-baseline.json")


@dataclass(frozen=True)
class Comment:
    path: str
    line: int
    kind: str
    text: str

    @property
    def fingerprint(self) -> str:
        value = f"{self.path}\0{self.kind}\0{self.text}"
        return hashlib.sha256(value.encode()).hexdigest()


def _python_comments(path: str, source: str) -> list[Comment]:
    out = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            out.append(Comment(path, token.start[0], "python", token.string))
    return out


def _scan_generic(path: str, source: str) -> list[Comment]:
    if Path(path).suffix == ".sh" and "node -e '" in source:
        marker = source.index("node -e '") + len("node -e '")
        close = source.rfind("\n'")
        if close > marker:
            script = source[marker:close]
            prefix_start = marker - len("node -e '")
            prefix = source[:prefix_start] + " " * len("node -e '")
            masked = prefix + "".join("\n" if ch == "\n" else " " for ch in script) + source[close:]
            shell_comments = _scan_generic(path, masked)
            js_comments = _scan_generic(f"{path}.js", script)
            offset = source[:marker].count("\n")
            return shell_comments + [
                Comment(path, comment.line + offset, comment.kind, comment.text)
                for comment in js_comments
            ]
    out: list[Comment] = []
    i = 0
    line = 1
    size = len(source)
    suffix = Path(path).suffix
    shell_or_config = (
        suffix in {".sh", ".yml", ".yaml", ".toml", ".conf"} or Path(path).name == "Dockerfile"
    )
    slash_comments = suffix in {
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".mjs",
        ".vue",
        ".sh",
    }
    dash_comments = suffix == ".sql"
    in_vue_script = False
    while i < size:
        char = source[i]
        if suffix == ".vue":
            if source.startswith("<script", i):
                in_vue_script = True
            elif source.startswith("</script", i):
                in_vue_script = False
        if char == "\n":
            line += 1
            i += 1
            continue
        if source.startswith("<!--", i):
            start, start_line = i, line
            end = source.find("-->", i + 4)
            if end < 0:
                end = size
                i = size
            else:
                i = end + 3
            raw = source[start:i]
            out.append(Comment(path, start_line, "html", raw))
            line += raw.count("\n")
            continue
        if source.startswith("/*", i):
            start, start_line = i, line
            end = source.find("*/", i + 2)
            if end < 0:
                end = size
                i = size
            else:
                i = end + 2
            raw = source[start:i]
            out.append(Comment(path, start_line, "block", raw))
            line += raw.count("\n")
            continue
        if slash_comments and (suffix != ".vue" or in_vue_script) and source.startswith("//", i):
            start, start_line = i, line
            end = source.find("\n", i)
            if end < 0:
                end = size
            raw = source[start:end]
            out.append(Comment(path, start_line, "line", raw))
            i = end
            continue
        if dash_comments and source.startswith("--", i):
            start, start_line = i, line
            end = source.find("\n", i)
            if end < 0:
                end = size
            raw = source[start:end]
            out.append(Comment(path, start_line, "dash", raw))
            i = end
            continue
        if shell_or_config and char == "#":
            start, start_line = i, line
            end = source.find("\n", i)
            if end < 0:
                end = size
            raw = source[start:end]
            out.append(Comment(path, start_line, "hash", raw))
            i = end
            continue
        if char in "'\"`":
            quote = char
            i += 1
            while i < size:
                if source[i] == "\\":
                    i += 2
                    continue
                if source[i] == "\n":
                    line += 1
                if source[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        i += 1
    return out


def scan(root: Path) -> list[Comment]:
    tracked = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    result = []
    for raw_path in tracked:
        if not raw_path:
            continue
        rel = raw_path.decode()
        file_path = root / rel
        if not file_path.is_file() or not (
            Path(rel).suffix in _SOURCE_SUFFIXES
            or Path(rel).name == "Dockerfile"
            or rel.endswith(".env.example")
        ):
            continue
        source = file_path.read_text(encoding="utf-8", errors="replace")
        if Path(rel).suffix == ".py":
            result.extend(_python_comments(rel, source))
        else:
            result.extend(_scan_generic(rel, source))
    return result


def check(root: Path) -> list[str]:
    baseline_path = root / _BASELINE
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    allowed = Counter(baseline["fingerprints"])
    errors = []
    for comment in scan(root):
        fingerprint = comment.fingerprint
        if allowed[fingerprint]:
            allowed[fingerprint] -= 1
        else:
            errors.append(f"New comment in {comment.path}:{comment.line}: {comment.text.strip()}")
    return errors


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    comments = scan(root)
    if "--list" in sys.argv:
        for comment in comments:
            print(f"{comment.path}:{comment.line}: {comment.kind}: {comment.text.strip()}")
        return 0
    errors = check(root)
    if errors:
        print("Comment ratchet FAILED:", file=sys.stderr)
        print("\n".join(f"  - {error}" for error in errors), file=sys.stderr)
        return 1
    print(f"Comment ratchet OK: {len(comments)} baseline comments remain.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
