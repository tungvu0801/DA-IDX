"""
fit — Stage 3.4 STRATEGY FIT: a read-only, on-demand comparison of CURRENT conditions with EXACT SAVED rules.

    readonly.py   read-only access to the Stage 3.1 / 3.2 / 3.3 tables (every connection is `mode=ro`)
    current.py    one evaluation: saved versions -> eligibility -> one shared feature environment for the symbol at
                  the latest completed close -> strategy.evaluate.group_met (via the Stage 3.3 trace helpers)
    evidence.py   stored Stage 3.2 backtests and Stage 3.3 forward journals of the EXACT version (kept separate)
    METHOD.md     rules, statuses, timing labels and known limitations

Question answered: for this stock, which saved strategy versions currently have entry rules that are met, which rules
are not met, and which required inputs are unavailable? It is not a recommendation, a ranking, a prediction or a
trade. Nothing here writes to the database, generates research, calls a broker or places an order.
"""
