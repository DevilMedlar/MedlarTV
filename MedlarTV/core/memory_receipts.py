"""In-process identities for memory admission and successful socket writes.

These objects contain no viewer message/profile values, except WrittenReply's
final body, which is transient and never logged by this contract.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryLease:
    store: object
    session: int
    viewer_session: int
    channel: str
    viewer: str


@dataclass(frozen=True)
class SourceObservation:
    lease: MemoryLease
    sequence: int


@dataclass(frozen=True)
class ReplyReceipt:
    owner: object
    generation: int
    control_revision: int
    lease: MemoryLease
    observation: SourceObservation | None
    profile_channel: str
    profile_viewer: str | None


@dataclass(frozen=True)
class WrittenReply:
    receipt: ReplyReceipt
    generation: int
    body: str
