"""RowanJobs: a durable longitudinal archive of Rowan University job advertisements.

The production collector makes zero model calls. Archived descriptions are never
summarised, rewritten or corrected -- see CLAUDE.md.
"""

__version__ = "1.0.0"

# Bumped whenever the meaning of an extraction changes -- including how links
# are classified, since that decides what the collector retrieves. 1.1.0 widened
# job documents from the career site to Rowan's own domain.
PARSER_VERSION = "1.1.0"
# Bumped whenever the rules that decide "is this content different?" change.
CONTRACT_VERSION = "1.0.0"
# Bumped whenever markup -> plain text rules change.
TEXT_CONTRACT_VERSION = "1.0.0"
# Bumped whenever the scan-qualification rules change.
QUALIFICATION_RULES_VERSION = "1.0.0"
# Bumped whenever derived presence/change event rules change.
EVENT_RULES_VERSION = "1.0.0"
# Version of the machine-readable health/status contract.
HEALTH_SCHEMA_VERSION = "1"

SOURCE_NAMESPACE = "rowan.pageup"
