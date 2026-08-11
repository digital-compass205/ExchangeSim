"""Browser view of one or more running simulators.

A separate process from the venues themselves: each venue still owns its own
port and store, and this connects to their control planes as a client. Nothing
here is on the order-entry path.
"""
