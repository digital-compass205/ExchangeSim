"""The HKEX OCG-C binary encoding of FIX 5.0 SP2.

HKEX publishes its Orion Central Gateway in two interchangeable wire formats:
tag=value FIX, and a fixed-width little-endian binary encoding. They describe
the *same* protocol -- same messages, same fields, same session rules -- so this
package is a codec, not a second gateway. It decodes a binary frame into the
:class:`~exchangesim.fix.message.Message` the venue already understands and
encodes one back, which is what lets the session layer, the dictionary, the
handlers, the audit and the web board serve both without knowing which is which.

Transcribed from HKEX_OCGC_Binary_Trading_Protocol_3.2 (19 July 2023).
"""
