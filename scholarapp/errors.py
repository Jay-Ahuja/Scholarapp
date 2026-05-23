"""Typed exceptions raised by Scholarapp modules.

The CLI layer catches `ScholarError` and renders a friendly message instead of a traceback.
Each pipeline module raises its own subclass so callers can distinguish failure modes.
"""

from __future__ import annotations


class ScholarError(Exception):
    """Base class for all Scholarapp errors."""


class ConfigError(ScholarError):
    """Configuration is invalid or a required value is missing."""


class NotFoundError(ScholarError):
    """A requested entity (run, draft, professor) does not exist."""


class IngestionError(ScholarError):
    """Failed to parse a resume PDF or prompt input (Step 3)."""


class DiscoveryError(ScholarError):
    """Failed to discover professors from external sources (Step 4)."""


class MatchingError(ScholarError):
    """Failed to match professor projects to user interests (Step 5)."""


class DraftingError(ScholarError):
    """Failed to draft an email (Step 6)."""


class ReviewError(ScholarError):
    """Failed to read or write draft files (Step 7)."""


class DeliveryError(ScholarError):
    """Failed to deliver an email or set up delivery credentials (Step 8)."""


class SendingDisabled(DeliveryError):
    """Sending is gated behind SEND_ENABLED=false. Raised by `scholar send` (Step 8)."""
