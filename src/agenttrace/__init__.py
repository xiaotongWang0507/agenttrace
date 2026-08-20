"""
AgentTrace - A tool for evaluating and tracing agent operations.
"""

# Import main classes to make them available at the top level
from .agenttrace import TraceManager
from .agenttrace import TracerEval
from .trajectory import (
    DynamicRedactor,
    GitVersionManager,
    PackagingApprovalRequired,
    TrajectoryBuilder,
)

# Version information
__version__ = "0.1.0"

__all__ = [
    "TraceManager",
    "TracerEval",
    "DynamicRedactor",
    "GitVersionManager",
    "PackagingApprovalRequired",
    "TrajectoryBuilder",
]
