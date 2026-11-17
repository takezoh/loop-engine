"""Tests of the provider transport: the sessions the engine opens with each agent's command.

Importing this package puts the engine's source directory on the import path, as `engine_support`
does for the tests beside it.
"""
import os
import sys

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
