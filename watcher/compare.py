import difflib
import re
from pathlib import Path

MARKER = re.compile(r"^// (?:File:? )?((?:[\w.@-]+/)*[\w.@-]+\.(?:sol|vy))\s*$", re.M)
COMMENTS_AND_STRINGS = re.compile(r"\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'|//[^\n]*|/\*.*?\*/", re.S)
LIMIT = 60_000


def split_files(folder: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(path for path in folder.rglob("*") if path.is_file()):
        text = path.read_bytes().decode(errors="replace").replace("\r\n", "\n")
        marks = list(MARKER.finditer(text))
        parts = {mark.group(1): text[mark.end():marks[index + 1].start() if index + 1 < len(marks) else len(text)] for index, mark in enumerate(marks)}
        if len(marks) > 1 and len(parts) == len(marks):
            files.update(parts)
            if code_lines(text[:marks[0].start()]):
                files["(header before the first file)"] = text[:marks[0].start()]
        else:
            files[str(path.relative_to(folder))] = text
    return files


def code_lines(text: str) -> list[str]:
    code = COMMENTS_AND_STRINGS.sub(lambda match: match.group(0) if match.group(0)[0] in "\"'" else "", text)
    return [line.strip() for line in code.splitlines() if line.strip()]


def compare(first: Path, second: Path) -> str:
    old, new = split_files(first), split_files(second)
    if len(old) == len(new) == 1 and old.keys() != new.keys():
        new = {next(iter(old)): next(iter(new.values()))}
    identical, diffs = 0, list[str]()
    for path in sorted(old.keys() & new.keys()):
        before, after = code_lines(old[path]), code_lines(new[path])
        if before == after:
            identical += 1
            continue
        diffs.append("\n".join(difflib.unified_diff(before, after, f"{first.name}/{path}", f"{second.name}/{path}", lineterm="")))
    only_first, only_second = sorted(old.keys() - new.keys()), sorted(new.keys() - old.keys())
    text = "\n".join([
        f"{identical} files are identical apart from comments and whitespace, {len(diffs)} files differ",
        f"only in {first.name}: {', '.join(only_first) or 'none'}",
        f"only in {second.name}: {', '.join(only_second) or 'none'}",
        "", *diffs,
    ])
    return text if len(text) <= LIMIT else text[:LIMIT] + f"\n... truncated after {LIMIT} characters"
