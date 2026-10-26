# list recipes
default:
    @just --list

# create/update the venv from pyproject.toml + uv.lock
sync:
    uv sync

# paper mode: live data, simulated fills
paper:
    uv run polytrader

# check creds, tick size and price bound; nothing is signed or sent
dry:
    uv run polytrader --dry

# real orders (starts disarmed: press a, then u/d)
live stake="0":
    uv run polytrader --live --stake {{stake}}

# offline suite: no credentials, no network
test:
    uv run pytest

# print the signer derived from PK and the USDC balance
doctor:
    uv run polytrader-doctor

# byte-compile the package
check:
    uv run python -m compileall -q src

clean:
    rm -rf .venv .pytest_cache
    find . -name __pycache__ -type d -prune -exec rm -rf {} +
