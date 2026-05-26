"""On-demand cash-buyer dispenser HTTP service.

Tranchi (or any other caller) POSTs a request like:

    POST /api/dispense
    { "user_id": "u_123", "market": "Cleveland OH", "quantity": 50 }

The service serves cache-bearing rows immediately and, if cache is short,
kicks off an async job to sync-batchdata + skip-trace the shortfall. The
caller polls /api/dispense/jobs/{id} for the final results.

Cost is reported as OUR raw BatchData cost (cost_usd). The caller applies
whatever markup their billing system uses (we do not charge end users).
"""

__all__ = ["api", "auth", "dispense", "jobs"]
