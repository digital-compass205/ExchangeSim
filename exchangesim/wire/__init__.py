"""What every wire protocol here shares, and nothing that belongs to one.

There are three protocol packages now -- :mod:`exchangesim.fix` (tag=value),
:mod:`exchangesim.binary` (HKEX's OCG-C binary encoding) and
:mod:`exchangesim.nnf` (NSE's native format) -- and a little of what they do is
genuinely common. That much lives here rather than in whichever package
happened to need it first, because a module importing ``fix`` for something that
is not FIX reads as a claim that it is.

The test for whether something belongs here is the same one ``CLAUDE.md``
applies to control commands: if it needs to know a dialect, it belongs to the
protocol; if it does not, it is shared. Value conversion between a field's
string form and its wire form does not.
"""
