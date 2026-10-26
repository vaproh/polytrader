"""CLI entry point: the TUI, with an optional live broker."""
from __future__ import annotations

import argparse

from .state import S
from .ui.app import TraderApp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="LIVE: orders are real. needs PK in env/.env")
    ap.add_argument("--dry", action="store_true",
                    help="checks credentials, tick size and price bound; "
                         "nothing is signed or sent")
    ap.add_argument("--stake", type=float, default=0.0,
                    help="dollar size per order; 0 starts disarmed (default)")
    a = ap.parse_args()
    if a.stake < 0:
        ap.error("--stake must be >= 0")
    S.stake = a.stake

    if a.live or a.dry:
        from .broker import LiveBroker
        S.broker = LiveBroker(dry_run=a.dry)
        S.live = bool(S.broker.ok)
        S.funder = S.broker.funder
        if not S.broker.ok:
            print(f"!! live requested but broker not ready: {S.broker.error}")
            print("!! starting in PAPER mode")
        else:
            S.balance = S.broker.balance()
            cap = f"${S.broker.max_usdc:.2f}" if S.broker.max_usdc > 0 else "no cap"
            print(f"{'DRY' if a.dry else 'LIVE'} signer={S.broker.addr} "
                  f"funder={S.broker.funder} max/order={cap}")
            if a.dry:
                print("DRY RUN: credentials checked; nothing is signed or sent")

    TraderApp().run()


if __name__ == "__main__":
    main()
