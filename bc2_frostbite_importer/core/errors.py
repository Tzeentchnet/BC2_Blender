# SPDX-License-Identifier: GPL-3.0-or-later


class BC2Error(RuntimeError):
    """Base error for recoverable importer failures."""


class CorruptDataError(BC2Error):
    """Raised when a disc or asset violates its declared binary layout."""


class UnsupportedFormatError(BC2Error):
    """Raised when an asset is valid but uses an unsupported format variant."""


class UnsupportedSourceError(BC2Error):
    """Raised when a selected source is not a supported BC2 disc image."""


class ExtractionError(BC2Error):
    """Raised when a container cannot provide a requested asset."""


class PlatformRequirementError(BC2Error):
    """Raised when an operation needs a facility unavailable on this OS."""
