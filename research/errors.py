"""The errors the harness raises, in one place.

Three of them, and the distinction between them is the point.

:class:`LeakageError` means the evaluation as set up would have produced a
number that looks like a result and is not one -- a split that puts tomorrow in
training, a model handed the test labels, a "feature" that is a transform of
the target. It is raised rather than warned about because Phase 2's exploratory
work produced exactly these numbers twice, and both times they were believed
for a while.

:class:`MetricError` means a metric was asked for something it cannot answer:
an accuracy over zero rows, a correlation between a sequence and itself of
length two. The harness never returns a plausible-looking number in that
situation, matching the Phase 2 rule that an absence is reported, not filled.

:class:`ReportError` means a result was about to be presented without the
comparison that makes it interpretable.
"""

from __future__ import annotations


class ResearchError(Exception):
    """Base class for everything this package raises."""


class LeakageError(ResearchError):
    """Future information was, or would have been, visible to a model."""


class MetricError(ResearchError):
    """A metric cannot be computed from the input it was given."""


class ReportError(ResearchError):
    """A result was about to be presented without its baseline."""


__all__ = ["LeakageError", "MetricError", "ReportError", "ResearchError"]
