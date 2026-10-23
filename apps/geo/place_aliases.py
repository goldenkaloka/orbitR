"""Crowd-sourced place names ("aliases") for areas with no reliable addressing.

Every saved address and every delivered order contributes the customer's own
words for a small H3 cell. A name becomes publicly searchable only when enough
distinct users agree on it, so a single person cannot rename a neighbourhood.
"""

import logging
import re

from apps.core.geo_utils import get_neighbors, h3_to_latlng, latlng_to_h3
from django.db import IntegrityError, transaction

from .models import PlaceAlias, PlaceAliasContributor

logger = logging.getLogger(__name__)

MIN_DISTINCT_USERS_FOR_PUBLIC = 3
MAX_PLACE_NAME_LENGTH = 60

# Deliberately small: false positives are worse than a moderator rejecting a
# single alias, and every public alias needs three independent users anyway.
_BLOCKED_TERMS = {
    'asshole',
    'bitch',
    'fuck',
    'nude',
    'porn',
    'shit',
}

_WHITESPACE = re.compile(r'\s+')
_EDGE_PUNCTUATION = ' .,;:!?·•|-'
_LEADING_DIGIT = re.compile(r'^\d')
_LONG_DIGIT_RUN = re.compile(r'\d{6,}')


def clean_place_name(raw):
    """Return the customer-written name, or ``None`` when it is not usable.

    Rejects house numbers, phone numbers and links so the alias cannot leak a
    specific household or contact detail.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    text = _WHITESPACE.sub(' ', text)
    text = text.strip(_EDGE_PUNCTUATION)
    if not text or len(text) > MAX_PLACE_NAME_LENGTH:
        return None
    if _LEADING_DIGIT.match(text) or _LONG_DIGIT_RUN.search(text):
        return None
    lowered = text.casefold()
    if 'http' in lowered or 'www.' in lowered or '@' in lowered:
        return None
    if any(term in lowered for term in _BLOCKED_TERMS):
        return None
    if text.isupper():
        text = text.title()
    return text


def search_key(query):
    """Normalised form used for both storage and lookup."""
    return _WHITESPACE.sub(' ', str(query or '').strip()).casefold()


def record_place_alias(*, h3_index, raw_name, user_id=None, zone=None):
    """Upsert one mention of ``raw_name`` for an H3 cell.

    Returns the alias, or ``None`` if the name was filtered out. Safe to call
    for repeat mentions by the same user: ``sample_count`` grows, but
    ``distinct_users`` only grows once per user.
    """
    display_text = clean_place_name(raw_name)
    if not display_text or not h3_index:
        return None

    key = search_key(display_text)
    if not key:
        return None

    for attempt in range(2):
        try:
            with transaction.atomic():
                alias, created = PlaceAlias.objects.select_for_update().get_or_create(
                    h3_index=h3_index,
                    text_normalized=key,
                    defaults={
                        'display_text': display_text,
                        'zone': zone,
                        'sample_count': 0,
                    },
                )
                alias.sample_count += 1
                if len(display_text) > len(alias.display_text):
                    alias.display_text = display_text
                if zone is not None and alias.zone_id is None:
                    alias.zone = zone

                if user_id is not None:
                    _, contributed = PlaceAliasContributor.objects.get_or_create(
                        alias=alias, user_id=user_id
                    )
                    if contributed:
                        alias.distinct_users += 1

                alias.confidence = round(min(1.0, alias.distinct_users / 5.0), 3)
                if (
                    alias.status == 'pending'
                    and alias.distinct_users >= MIN_DISTINCT_USERS_FOR_PUBLIC
                ):
                    alias.status = 'approved'
                alias.save()
                return alias
        except IntegrityError:
            # Two workers inserted the same (cell, name) pair at once.
            if attempt == 0:
                continue
            logger.warning(
                'Could not record place alias %r for cell %s', display_text, h3_index
            )
            return None
    return None


def record_address_place_alias(address):
    if address is None:
        return None
    h3_index = address.h3_index
    if not h3_index and address.delivery_location:
        h3_index = latlng_to_h3(
            address.delivery_location.y, address.delivery_location.x
        )
    return record_place_alias(
        h3_index=h3_index,
        raw_name=address.place_name,
        user_id=address.user_id,
        zone=address.zone,
    )


def _first_address_segment(delivery_address):
    if not delivery_address:
        return None
    for separator in ('·', ',', ' - '):
        if separator in delivery_address:
            return delivery_address.split(separator)[0]
    return delivery_address


def record_order_place_alias(order):
    """Learn from a delivered order's address.

    The app sends "<place name> · <street> · <landmark>", so the first segment
    is the customer's own name for the area.
    """
    if order is None or order.fulfillment_type != 'DELIVERY':
        return None
    h3_index = order.h3_index
    if not h3_index and order.delivery_location:
        h3_index = latlng_to_h3(
            order.delivery_location.y, order.delivery_location.x
        )
    if not h3_index:
        return None
    zone = None
    try:
        from apps.core.geo_utils import get_zone_from_h3

        zone = get_zone_from_h3(h3_index)
    except Exception:
        zone = None
    return record_place_alias(
        h3_index=h3_index,
        raw_name=_first_address_segment(order.delivery_address),
        user_id=order.customer_id,
        zone=zone,
    )


def search_aliases(query, limit=5):
    key = search_key(query)
    if len(key) < 3:
        return []
    return list(
        PlaceAlias.objects.filter(
            status='approved', text_normalized__contains=key
        )
        .select_related('zone')
        .order_by('-distinct_users', '-confidence', '-last_seen')[:limit]
    )


def aliases_near_cells(h3_cells, limit=1):
    if not h3_cells:
        return []
    return list(
        PlaceAlias.objects.filter(
            status='approved', h3_index__in=list(h3_cells)
        )
        .select_related('zone')
        .order_by('-distinct_users', '-confidence', '-last_seen')[:limit]
    )


def alias_coordinates(alias):
    try:
        return h3_to_latlng(alias.h3_index)
    except Exception:
        return None


def cell_and_neighbours(h3_index):
    try:
        return [h3_index, *get_neighbors(h3_index)]
    except Exception:
        return [h3_index]


def trigger_address_alias_learning(address_id):
    """Queue alias learning without ever failing the customer's request."""
    try:
        from .tasks import learn_address_place_alias

        learn_address_place_alias.delay(str(address_id))
    except Exception as exc:  # pragma: no cover - broker issues are non-fatal
        logger.warning('Could not queue place-alias learning: %s', exc)
