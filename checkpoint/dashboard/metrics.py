"""Tiny Prometheus exposition format. Avoids the prometheus_client dep.

We export four metrics. Add more here as needed; the format is plain text:
    # HELP <name> <description>
    # TYPE <name> <type>
    <name>{labels} value
"""
from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock


class Metrics:
    def __init__(self) -> None:
        self._http_total: dict[tuple[str, str, int], int] = defaultdict(int)
        self._http_duration_sum: dict[tuple[str, str], float] = defaultdict(float)
        self._http_duration_count: dict[tuple[str, str], int] = defaultdict(int)
        self._jobs_total: dict[str, int] = defaultdict(int)
        self._sse_subscribers = 0
        self._lock = Lock()
        self._start = time.time()

    def observe_http(self, method: str, path: str, status: int, duration_s: float) -> None:
        with self._lock:
            self._http_total[(method, path, status)] += 1
            self._http_duration_sum[(method, path)] += duration_s
            self._http_duration_count[(method, path)] += 1

    def inc_job(self, status: str) -> None:
        with self._lock:
            self._jobs_total[status] += 1

    def set_sse_subscribers(self, n: int) -> None:
        with self._lock:
            self._sse_subscribers = n

    def render(self) -> str:
        with self._lock:
            lines: list[str] = []
            lines.append("# HELP checkpoint_uptime_seconds Time since dashboard started.")
            lines.append("# TYPE checkpoint_uptime_seconds gauge")
            lines.append(f"checkpoint_uptime_seconds {time.time() - self._start:.1f}")

            lines.append("# HELP checkpoint_http_requests_total Total HTTP requests.")
            lines.append("# TYPE checkpoint_http_requests_total counter")
            for (method, path, status), n in self._http_total.items():
                lines.append(
                    f'checkpoint_http_requests_total{{method="{method}",path="{_label(path)}",status="{status}"}} {n}'
                )

            lines.append("# HELP checkpoint_http_request_duration_seconds Avg request duration.")
            lines.append("# TYPE checkpoint_http_request_duration_seconds gauge")
            for (method, path), s in self._http_duration_sum.items():
                count = self._http_duration_count[(method, path)] or 1
                lines.append(
                    f'checkpoint_http_request_duration_seconds{{method="{method}",path="{_label(path)}"}} {s / count:.6f}'
                )

            lines.append("# HELP checkpoint_jobs_total Total run jobs by terminal status.")
            lines.append("# TYPE checkpoint_jobs_total counter")
            for status, n in self._jobs_total.items():
                lines.append(f'checkpoint_jobs_total{{status="{status}"}} {n}')

            lines.append("# HELP checkpoint_sse_subscribers Currently connected SSE clients.")
            lines.append("# TYPE checkpoint_sse_subscribers gauge")
            lines.append(f"checkpoint_sse_subscribers {self._sse_subscribers}")

            return "\n".join(lines) + "\n"


def _label(s: str) -> str:
    """Sanitize a path label so it's safe in Prometheus exposition format."""
    return s.replace('"', "").replace("\\", "")
