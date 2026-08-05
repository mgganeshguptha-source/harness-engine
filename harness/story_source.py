"""
story_source.py — where the harness gets the user story.

A clean seam so the *source* of the story is swappable without touching the rest
of the harness:
  - FileStorySource  : reads a markdown file (demo; an MCP step would populate it)
  - JiraMcpStorySource: (future) fetch from Jira via MCP — same interface

In production, a pre-step (MCP -> Jira) writes the story file; the harness just
reads it. The harness does not care HOW the story arrived. Today we read a
committed file so CI can see it.
"""
from __future__ import annotations
from pathlib import Path
from typing import Protocol


class StorySource(Protocol):
    def get_story(self) -> str: ...


class FileStorySource:
    """Reads the story from a markdown file (repo-relative or absolute)."""
    def __init__(self, path: Path):
        self.path = Path(path)

    def get_story(self) -> str:
        if not self.path.exists():
            raise FileNotFoundError(
                f"Story file not found: {self.path}. "
                f"In production an MCP/Jira step writes this; for the demo, create it."
            )
        text = self.path.read_text(encoding="utf-8").strip()
        if not text:
            raise ValueError(f"Story file is empty: {self.path}")
        return text


# Future:
# class JiraMcpStorySource:
#     def __init__(self, issue_key, mcp_client): ...
#     def get_story(self) -> str:
#         # call Jira via MCP, return the formatted story
#         ...
