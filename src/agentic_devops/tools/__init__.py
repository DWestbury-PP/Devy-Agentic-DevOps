"""Tools and the on-demand tools-router — the framework's differentiator.

Tools register with rich metadata but are NOT all dumped into the model's
context. The agent starts with a single ``find_tools`` capability and discovers
the relevant tools on demand, keeping the working context small while reach
stays broad. See docs/JOURNEY.md (Pivot 3).
"""

from agentic_devops.tools.base import ToolSpec
from agentic_devops.tools.router import FIND_TOOLS_NAME, ToolsRouter

__all__ = ["ToolSpec", "ToolsRouter", "FIND_TOOLS_NAME"]
