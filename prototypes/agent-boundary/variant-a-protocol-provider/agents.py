"""Sample agent declarations for variant A.

A ``code_reviewer`` agent that:

* binds output to ``ReviewVerdict`` (typed structured output)
* exposes two tools (``read_file`` and ``count_lines``)
* runs on claude-haiku-4-5 in production (override per call site)
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from provider import AgentSpec, Tool


# ---- output schema ---------------------------------------------------------


class ReviewFinding(BaseModel):
    severity: str = Field(pattern="^(blocking|nit|info)$")
    line: int = Field(ge=1)
    message: str


class ReviewVerdict(BaseModel):
    summary: str
    findings: list[ReviewFinding]
    recommend_merge: bool


# ---- tool implementations --------------------------------------------------


class ReadFileArgs(BaseModel):
    path: str = Field(description="repo-relative file path")


class CountLinesArgs(BaseModel):
    path: str


# Real impls would touch disk; the demo stubs them so they are pure.
_VIRTUAL_FILES: dict[str, str] = {
    "src/auth.py": "def login(u, p):\n    return True  # TODO: real auth\n",
}


def read_file(path: str) -> str:
    return _VIRTUAL_FILES.get(path, f"<no such file: {path}>")


def count_lines(path: str) -> int:
    return len(_VIRTUAL_FILES.get(path, "").splitlines())


READ_FILE = Tool(
    name="read_file",
    description="Return the contents of a repo-relative path.",
    parameters=ReadFileArgs,
    fn=read_file,
)

COUNT_LINES = Tool(
    name="count_lines",
    description="Return the line count of a repo-relative path.",
    parameters=CountLinesArgs,
    fn=count_lines,
)


# ---- agent spec ------------------------------------------------------------


CODE_REVIEWER = AgentSpec(
    name="code_reviewer",
    system=(
        "You review small Python diffs. Use the tools to inspect the file. "
        "Return JSON matching the ReviewVerdict schema."
    ),
    response_model=ReviewVerdict,
    model="claude-haiku-4-5",
    tools=(READ_FILE, COUNT_LINES),
)
