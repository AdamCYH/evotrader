"""Entry point for `python -m evotrader`."""

import warnings

# ADK announces each experimental feature it turns on, at import time. That is
# for ADK's developers; to someone starting EvoTrader it reads like a fault.
warnings.filterwarnings("ignore", message=r"\[EXPERIMENTAL\] feature", category=UserWarning)

from evotrader.main import run  # noqa: E402  (after the filter: the import warns)

if __name__ == "__main__":
    run()
