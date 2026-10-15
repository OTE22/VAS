"""Bounded Quick Search inputs and exact, filtered per-person ranking."""
from datetime import datetime, timezone, time
from io import BytesIO
import warnings
from PIL import Image
from fastapi import HTTPException


def search_dates(date_from, date_to):
    def parse(value):
        if not value:
            return None
        try:
            result = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return result.astimezone(timezone.utc).replace(tzinfo=None) if result.tzinfo else result
        except (ValueError, TypeError):
            raise HTTPException(422, 'Dates must use ISO date/time format.')
    start, end = parse(date_from), parse(date_to)
    # A date-only upper bound includes the entire UTC calendar day. Browser
    # callers send explicit UTC instants for their local calendar boundaries.
    if end is not None and len(date_to) == 10:
        end = datetime.combine(end.date(), time.max)
    if start and end and start > end:
        raise HTTPException(422, 'Start date must not be later than end date.')
    return start, end


def decode_search_image(payload):
    from backend.core.enrollment_service import (decode_and_validate_image, EnrollmentError,
                                               MAX_IMAGE_SIDE, MAX_DECODED_PIXELS)
    # Inspect dimensions before OpenCV allocates the decoded pixel buffer.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload)) as header:
                width, height = header.size
                if max(width, height) > MAX_IMAGE_SIDE or width * height > MAX_DECODED_PIXELS:
                    raise HTTPException(413, 'Image resolution is too large to process.')
    except HTTPException:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(413, 'Image resolution is too large to process.')
    except Exception:
        raise HTTPException(400, 'Invalid image file. Upload a JPG, PNG or WEBP image.')
    try:
        return decode_and_validate_image(payload)
    except EnrollmentError as exc:
        raise HTTPException(400, str(exc))


