"""Backward-compatibility shim — imports from delegate.workflows.core.

.. deprecated:: Use ``delegate.workflows.core`` directly.
"""

# Re-export everything so existing code (task.py, web.py) keeps working.
from delegate.workflows.core import (  # noqa: F401
    Context,
    TaskView,
    AgentInfo,
)

# Also import git mixin so Context has git methods available
import delegate.workflows.git  # noqa: F401

# Legacy names from the old lib.py
from delegate.workflows.git import MergeResult as MergeResultView  # noqa: F401
from delegate.workflows.git import TestResult  # noqa: F401
