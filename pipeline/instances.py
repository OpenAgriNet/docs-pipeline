"""Instance (state / tenant) code → human-readable name.

``documents.instance`` stores a short lowercase code — ``bv`` for the Bharat
Vistaar portal, otherwise the state code matching the Keycloak group path
(``/states/MH/...`` → ``mh``). That code is what the pipeline filters on, but
it is not what a reader wants to see: consumers of the vector payload and the
master catalog get ``instance_name`` alongside it so they can show "Maharashtra"
without shipping their own lookup table.

Codes follow the standard two-letter Indian state/UT abbreviations. Unknown
codes fall back to a title-cased version of the code itself rather than raising
— a new state should degrade to a readable label, not break ingestion.
"""

from __future__ import annotations

import os

# Portal / platform-wide documents (not a state).
PORTAL_INSTANCE = "bv"


def prod_stage_disabled() -> bool:
    """True when DISABLE_PROD_SETTING turns off the PROD half of the pipeline.

    With this set, a document finishes at DEV ingest instead of waiting for a
    superadmin to promote it — the ``approval_for_prod`` and ``ingesting_prod``
    stages are skipped entirely.

    Read only when a workflow is *started*; the value is then passed into the
    workflow as an argument. Reading it inside a workflow would break Temporal
    replay, because flipping the flag mid-flight would send a replaying
    workflow down a different branch than the one recorded in its history.
    """
    return (os.environ.get("DISABLE_PROD_SETTING", "false").strip().lower()
            in {"true", "1", "yes"})

INSTANCE_NAMES: dict[str, str] = {
    # Platform
    "bv": "Bharat Vistaar",
    "default": "Default",
    # States
    "ap": "Andhra Pradesh",
    "ar": "Arunachal Pradesh",
    "as": "Assam",
    "br": "Bihar",
    "cg": "Chhattisgarh",
    "ga": "Goa",
    "gj": "Gujarat",
    "hr": "Haryana",
    "hp": "Himachal Pradesh",
    "jh": "Jharkhand",
    "ka": "Karnataka",
    "kl": "Kerala",
    "mp": "Madhya Pradesh",
    "mh": "Maharashtra",
    "mn": "Manipur",
    "ml": "Meghalaya",
    "mz": "Mizoram",
    "nl": "Nagaland",
    "od": "Odisha",
    "or": "Odisha",  # older code still seen in group paths
    "pb": "Punjab",
    "rj": "Rajasthan",
    "sk": "Sikkim",
    "tn": "Tamil Nadu",
    "tg": "Telangana",
    "ts": "Telangana",  # older code still seen in group paths
    "tr": "Tripura",
    "up": "Uttar Pradesh",
    "uk": "Uttarakhand",
    "ua": "Uttarakhand",  # older code still seen in group paths
    "wb": "West Bengal",
    # Union territories
    "an": "Andaman and Nicobar Islands",
    "ch": "Chandigarh",
    "dn": "Dadra and Nagar Haveli and Daman and Diu",
    "dl": "Delhi",
    "jk": "Jammu and Kashmir",
    "la": "Ladakh",
    "ld": "Lakshadweep",
    "py": "Puducherry",
}


def instance_display_name(code: str | None) -> str:
    """Human-readable name for an instance code (``mh`` → ``Maharashtra``).

    Unknown codes are title-cased so the field is never empty.
    """
    normalized = (code or "").strip().lower()
    if not normalized:
        return INSTANCE_NAMES["default"]
    known = INSTANCE_NAMES.get(normalized)
    if known:
        return known
    # Unknown code: make it presentable rather than failing the ingest.
    return normalized.replace("-", " ").replace("_", " ").title()
