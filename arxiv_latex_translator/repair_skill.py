"""The repair skill: accumulated debugging experience for translated arXiv LaTeX builds.

The skill is a Codex-style `SKILL.md`. The repository ships the curated seed; the live copy lives
in the data directory (outside Git), is injected into every compile-repair prompt, and gains one
"Learned cases" entry after each successful repair that taught something new.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
from pathlib import Path

SEED = Path(__file__).parent / "skills" / "arxiv-latex-repair" / "SKILL.md"
PROMPT_LIMIT = 24000
_lock = threading.Lock()


def skill_file(base_dir: Path) -> Path:
    configured = os.environ.get("ARXIV_REPAIR_SKILL_DIR", "").strip()
    directory = Path(configured).expanduser() if configured else base_dir.parent / "skills" / "arxiv-latex-repair"
    return directory / "SKILL.md"


def load_skill(base_dir: Path) -> str:
    path = skill_file(base_dir)
    with _lock:
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SEED, path)
        return path.read_text(encoding="utf-8")


def skill_for_prompt(base_dir: Path) -> str:
    """The skill text, keeping the curated head and the newest learned cases when it is long."""

    text = load_skill(base_dir)
    if len(text) <= PROMPT_LIMIT:
        return text
    head = text[: PROMPT_LIMIT // 3]
    tail = text[-(PROMPT_LIMIT - len(head)) :]
    return head + "\n\n[... older learned cases omitted ...]\n\n" + tail


def lesson_prompt(skill: str, problems: str, repairs: str) -> str:
    return f"""Role: you maintain a debugging playbook for building Chinese translations of arXiv LaTeX
papers with XeTeX (Tectonic). Engineers and agents read it before repairing a broken build.

Task: a build problem was just repaired. Decide whether it teaches something the playbook does not
already cover. The playbook is only for problems that need an agent's judgment to repair inside a
paper's LaTeX: package conflicts, engine incompatibilities, author-specific macros, bibliography
setups, and LaTeX damaged by translation. Answer exactly <lesson>NONE</lesson> when:
- an existing section or learned case already describes the same symptom and fix, or
- the cause is a defect of the translation pipeline itself (its tokenizer, placeholder handling,
  sentence splitting, validation, or compiler invocation) rather than of this paper's LaTeX;
  such defects are fixed once in the pipeline code, not by agents paper by paper.
Otherwise write one new case that generalizes beyond this
paper (no paper-specific file names unless they are standard, like `main.bbl`), in this format:

<lesson>
### <short title>
- Symptom: <exact log message or build-check finding>
- Cause: <why it happens>
- Fix: <the edit that worked, as a general rule>
- Avoid: <a tempting fix that is wrong, if any>
</lesson>

Current playbook:
{skill}

Problems seen in this build:
{problems}

Repairs that were applied:
{repairs}
"""


def parse_lesson(output: str) -> str | None:
    match = re.search(r"<lesson>\s*(.*?)\s*</lesson>", output, re.S)
    if not match:
        return None
    lesson = match.group(1).strip()
    if not lesson or lesson.upper() == "NONE" or not lesson.startswith("###"):
        return None
    return lesson


def append_lesson(base_dir: Path, lesson: str, *, source: str) -> None:
    load_skill(base_dir)
    path = skill_file(base_dir)
    stamp = time.strftime("%Y-%m-%d")
    entry = f"\n{lesson.rstrip()}\n- Learned: {stamp} from {source}\n"
    with _lock:
        text = path.read_text(encoding="utf-8")
        temp = path.with_suffix(".tmp")
        temp.write_text(text.rstrip() + "\n" + entry, encoding="utf-8")
        temp.replace(path)
