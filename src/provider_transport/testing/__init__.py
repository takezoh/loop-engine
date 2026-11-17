"""Doubles of the transport's ports for the tests of whoever uses them.

Each one runs the same contract tests as the adapters it stands in for (`tests/test_port_contracts.py`
lists them).
"""
from .cell_fake import FakeCellMembership
from .directory_fake import FakeCwdAuthority

__all__ = ["FakeCellMembership", "FakeCwdAuthority"]
