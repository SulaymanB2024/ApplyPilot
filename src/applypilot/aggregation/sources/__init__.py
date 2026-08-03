"""Source adapters for progressive aggregation."""

from applypilot.aggregation.sources.base import SourceAdapter
from applypilot.aggregation.sources.cache import CacheSource
from applypilot.aggregation.sources.direct_ats import DirectATSSource
from applypilot.aggregation.sources.jobspy import JobSpySettings, JobSpySource
from applypilot.aggregation.sources.manual_import import ManualImportSource
from applypilot.aggregation.sources.smart_extract import SmartExtractSource
from applypilot.aggregation.sources.workday import WorkdaySource

__all__ = [
    "CacheSource",
    "DirectATSSource",
    "JobSpySettings",
    "JobSpySource",
    "ManualImportSource",
    "SmartExtractSource",
    "SourceAdapter",
    "WorkdaySource",
]
