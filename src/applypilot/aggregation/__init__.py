"""Progressive, observable job aggregation."""

from applypilot.aggregation.models import (
    AggregationRequest,
    CanonicalJob,
    RawJob,
    SourceKind,
)

__all__ = ["AggregationRequest", "CanonicalJob", "RawJob", "SourceKind"]
