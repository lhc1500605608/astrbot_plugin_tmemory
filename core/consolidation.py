"""Memory consolidation pipeline — facade re-exports.

See submodules for implementation:
- core/episode_manager.py — Stage B: EpisodeManager
- core/semantic_extractor.py — Stage C: SemanticExtractor
- core/consolidation_runtime.py — ConsolidationRuntimeMixin
- core/profile_extractor.py — ProfileExtractor
- core/profile_extraction_runtime.py — ProfileExtractionRuntimeMixin
"""

from .consolidation_runtime import ConsolidationRuntimeMixin
from .episode_manager import EpisodeManager
from .profile_extraction_runtime import ProfileExtractionRuntimeMixin
from .profile_extractor import ProfileExtractor
from .semantic_extractor import SemanticExtractor

__all__ = [
    "ConsolidationRuntimeMixin",
    "EpisodeManager",
    "ProfileExtractionRuntimeMixin",
    "ProfileExtractor",
    "SemanticExtractor",
]
