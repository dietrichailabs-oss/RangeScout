"""RangeScout application package."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ProductIdentity:
    name: str = "RangeScout"
    company: str = "Dietrich AI Labs"
    version: str = "1.6.1"
    build_identity: str = "rs-v1.6.1-quote-priority-eng2"
    publisher: str = "Dietrich AI Labs"


PRODUCT = ProductIdentity()
