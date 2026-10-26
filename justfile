# list recipes
default:
    @just --list

# create/update the venv from pyproject.toml + uv.lock
sync:
    uv sync

# paper mode: live data, simulated fills
paper:
    uv run python trader.py

# check creds, tick size and price bound; nothing is signed or sent
dry:
    uv run python trader.py --dry

# real orders
live stake="5":
    uv run python trader.py --live --stake {{stake}}

# offline suite: no credentials, no network
test:
    uv run pytest

# print the signer derived from PK and the USDC balance
doctor:
    uv run python execution.py

# byte-compile everything
check:
    uv run python -m py_compile trader.py execution.py pm_tools.py

clean:
    rm -rf .venv .pytest_cache __pycache__ tests/__pycache__
