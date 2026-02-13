"""Built-in workflow definitions and context for Delegate.

- ``delegate.workflows.core`` — VCS-agnostic Context and TaskView
- ``delegate.workflows.git``  — Git-specific operations (worktrees, reviews, merge)
- ``delegate.workflows.default`` — Default software development workflow
"""

# Apply git mixin to Context eagerly so it's always available
import delegate.workflows.git  # noqa: F401
