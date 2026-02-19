"""ORCID public API client for retrieving researcher name."""

import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

ORCID_API_BASE = "https://pub.orcid.org/v3.0"


def fetch_orcid_name(orcid_id: str) -> Optional[str]:
    """Fetch a researcher's first name from the ORCID public API.

    Args:
        orcid_id: ORCID identifier (e.g., '0000-0002-1234-5678').

    Returns:
        First name string, or None if lookup fails.
    """
    if not orcid_id or not orcid_id.strip():
        return None

    orcid_id = orcid_id.strip()
    url = f"{ORCID_API_BASE}/{orcid_id}/person"
    headers = {"Accept": "application/json"}

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        name_data = data.get("name", {})
        if name_data is None:
            return None

        # Prefer credit-name — this is the researcher's chosen display name
        credit = name_data.get("credit-name")
        if credit and isinstance(credit, dict):
            credit_name = credit.get("value", "")
            if credit_name:
                # Extract the first word as the first name for greeting
                first = credit_name.strip().split()[0]
                return first

        # Fall back to given-names
        given = name_data.get("given-names", {})
        if given is None:
            given = {}
        family = name_data.get("family-name", {})
        if family is None:
            family = {}

        given_name = given.get("value", "")
        family_name = family.get("value", "")

        if given_name:
            return given_name
        if family_name:
            return family_name
        return None

    except requests.exceptions.RequestException as e:
        logger.warning(f"ORCID lookup failed for {orcid_id}: {e}")
        return None
    except (KeyError, ValueError, TypeError) as e:
        logger.warning(f"ORCID response parsing error for {orcid_id}: {e}")
        return None
