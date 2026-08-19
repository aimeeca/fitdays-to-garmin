#!/usr/bin/env python3
"""
Import a body-composition history (Fitdays CSV export) into Garmin Connect.

Setup:
    pip install -r requirements.txt
    export GARMIN_EMAIL='you@example.com'
    export GARMIN_PASSWORD='your-password'      # single quotes: shell-safe

Usage:
    python garmin_import.py scalehistory.csv --dry-run   # show what would be uploaded
    python garmin_import.py scalehistory.csv             # upload for real
    python garmin_import.py scalehistory.csv --force     # upload even dates already on Garmin

Dates already present on Garmin are skipped, so the script is idempotent and
safe to re-run.

The session token is cached in ~/.garminconnect: after the first login (MFA
included) later runs need neither credentials nor a code.
"""

import argparse
import csv
import os
import sys
import time
from datetime import datetime

HEIGHT_M = float(os.environ.get("HEIGHT_M", "1.70"))  # used for BMI
WEIGH_HOUR = "07:00:00"  # CSV has dates only; pick a plausible time of day
TOKENSTORE = os.path.expanduser("~/.garminconnect")
SLEEP = 0.5              # pause between uploads (rate limit)


def to_float(v):
    v = (v or "").strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def load_rows(path):
    """Read the CSV; for each date keep the most complete row."""
    by_date = {}
    skipped = 0

    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            date = (row.get("date") or "").strip()
            weight = to_float(row.get("weight_kg"))
            if not date or weight is None:
                skipped += 1
                continue

            entry = {
                "date": date,
                "weight": weight,
                "percent_fat": to_float(row.get("body_fat_pct")),
                "muscle_mass": to_float(row.get("muscle_mass_kg")),
                "visceral_fat_rating": to_float(row.get("visceral_fat")),
                "percent_hydration": to_float(row.get("body_water_pct")),
                "basal_met": to_float(row.get("bmr_kcal")),
            }
            entry["filled"] = sum(1 for v in entry.values() if v is not None)

            # dedup: the row with more populated fields wins
            prev = by_date.get(date)
            if prev is None or entry["filled"] > prev["filled"]:
                by_date[date] = entry

    return [by_date[d] for d in sorted(by_date)], skipped


def payload(entry):
    ts = f"{entry['date']}T{WEIGH_HOUR}"
    datetime.fromisoformat(ts)  # validate date, fail fast if malformed

    p = {
        "timestamp": ts,
        "weight": entry["weight"],
        "bmi": round(entry["weight"] / (HEIGHT_M ** 2), 1),
    }
    for k in ("percent_fat", "percent_hydration", "muscle_mass",
              "basal_met", "visceral_fat_rating"):
        if entry[k] is not None:
            p[k] = entry[k]
    return p


def connect():
    """Log in with the cached token, falling back to email/password (+ MFA)."""
    from garminconnect import Garmin

    api = Garmin()
    try:
        api.login(TOKENSTORE)
        print("Session resumed from cached token.")
        return api
    except Exception:
        pass

    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    if not (email and password):
        sys.exit("First login needed: export GARMIN_EMAIL and GARMIN_PASSWORD.")
    api = Garmin(email, password,
                 prompt_mfa=lambda: input("Garmin MFA code: ").strip())
    api.login(TOKENSTORE)  # also persists the token to TOKENSTORE
    print(f"Login OK, token saved to {TOKENSTORE}")
    return api


def fetch_existing(api, first_date, last_date):
    """Weights already on Garmin in the range: {'YYYY-MM-DD': kg}.

    Queries one year at a time: the endpoint truncates very long ranges.
    """
    existing = {}
    for year in range(int(first_date[:4]), int(last_date[:4]) + 1):
        start = max(f"{year}-01-01", first_date)
        end = min(f"{year}-12-31", last_date)
        try:
            data = api.get_body_composition(start, end)
        except Exception as err:
            print(f"  ! {year}: read failed ({err}) — no skipping for this year")
            continue
        for item in (data or {}).get("dateWeightList", []):
            date = item.get("calendarDate")
            grams = item.get("weight")
            if date:
                existing[date] = round(grams / 1000, 2) if grams else None
    return existing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="upload even dates already present on Garmin")
    args = ap.parse_args()

    rows, skipped = load_rows(args.csv_path)
    if not rows:
        sys.exit("No valid rows in the CSV.")

    print(f"{len(rows)} unique weigh-ins  |  {skipped} rows discarded")
    print(f"range: {rows[0]['date']} → {rows[-1]['date']}\n")

    api = connect()

    existing = {}
    if not args.force:
        print("Checking what is already on Garmin...")
        existing = fetch_existing(api, rows[0]["date"], rows[-1]["date"])
        print(f"{len(existing)} weigh-ins already present in range\n")

    todo, skip = [], []
    for e in rows:
        if e["date"] in existing:
            skip.append((e, existing[e["date"]]))
        else:
            todo.append(e)

    for e, garmin_kg in skip:
        diff = "" if garmin_kg is None or abs(garmin_kg - e["weight"]) < 0.05 \
               else f"  (CSV {e['weight']} ≠ Garmin {garmin_kg})"
        print(f"  = {e['date']}  already present{diff}")

    print(f"\nTo upload: {len(todo)}  |  Skipped: {len(skip)}")

    if args.dry_run:
        print()
        for e in todo:
            print(payload(e))
        print("\n[dry-run] nothing uploaded.")
        return

    if not todo:
        print("Nothing to do.")
        return

    print()
    ok = fail = 0
    for e in todo:
        try:
            api.add_body_composition(**payload(e))
            ok += 1
            print(f"  ✓ {e['date']}  {e['weight']} kg")
        except Exception as err:
            fail += 1
            print(f"  ✗ {e['date']}  {err}")
        time.sleep(SLEEP)

    print(f"\nUploaded {ok}/{len(todo)}" + (f", {fail} failed" if fail else ""))


if __name__ == "__main__":
    main()
