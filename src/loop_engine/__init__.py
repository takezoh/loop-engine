"""Loop Engine: drives a unit on the host until every required fact of its contract holds.

The engine starts only git and the installed agent commands. It never runs a
project command, owns no sandbox and passes agents no sandbox or approval
setting; project code runs inside each agent's own sandbox.
"""
