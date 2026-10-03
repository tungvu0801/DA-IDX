"""Stage 3.8 GROUNDED AI EXPLANATION of an already-computed Strategy Fit result or Evidence view.

Python decides every status, rule result and number; Claude only explains the compact deterministic payload, and only
after an explicit click (at most 1 Claude call per action). Trivial states get a local explanation with no AI call.
Nothing here writes to the database, fetches market data, reads a broker account, recommends, ranks, predicts or
changes a strategy.
"""
