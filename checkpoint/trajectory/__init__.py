"""Trajectory-level evaluation.

Scoring only the final state is optimistic: an agent can reach the right end
state through a wrong, wasteful, or unsafe path — redundant writes, a forbidden
call it later undid, ten tries where one would do. The trajectory is the
sequence of API calls the agent actually made (from each twin's /_trace), and
`[T]` criteria assert properties of that path, deterministically and for free.
"""
from .model import Trajectory, TrajectoryStep
from .metrics import TrajectoryMetrics, compute_metrics

__all__ = ["Trajectory", "TrajectoryStep", "TrajectoryMetrics", "compute_metrics"]
