#!/usr/bin/env python3
"""
Synchronise body-composition measurements automatically from Fitdays
to Garmin Connect.

Environment variables:
    FITDAYS_EMAIL
    FITDAYS_PASSWORD
    GARMIN_EMAIL
    GARMIN_PASSWORD

Optional:
    FITDAYS_PROFILE
    HEIGHT_M
"""

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime

HEIGHT_M = float(os.environ.get("HEIGHT_M", "1.70"))
FITDAYS_PROFILE = os.environ.get("FITDAYS_PROFILE", "").strip()

TOKENSTORE = os.path.expanduser("~/.garminconnect")
WEIGH_HOUR = "07:00:00"
SLEEP = 0.5


def measurement_date(measurement):
    """
    Extract the measurement date while tolerating minor differences
    between Fitdays client versions.
    """
    for attribute in (
        "measured_at",
        "timestamp",
        "datetime",
        "created_at",
        "date",
        "time",
    ):
        value = getattr(measurement, attribute, None)

        if value is None:
            continue

        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d")

        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value).strftime("%Y-%m-%d")

        value = str(value).strip()

        if value.isdigit():
            timestamp = int(value)

            # Milliseconds instead of seconds
            if timestamp > 10_000_000_000:
                timestamp /= 1000

            return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")

        try:
            return datetime.fromisoformat(
                value.replace("Z", "+00:00")
            ).strftime("%Y-%m-%d")
        except ValueError:
            pass

        # Handles strings beginning with YYYY-MM-DD
        if len(value) >= 10:
            candidate = value[:10]

            try:
                datetime.strptime(candidate, "%Y-%m-%d")
                return candidate
            except ValueError:
                pass

    raise ValueError(f"No usable date found in measurement: {measurement!r}")


def value_or_none(measurement, attribute):
    value = getattr(measurement, attribute, None)

    if value is None:
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def load_fitdays():
    """Download all measurements from the Fitdays cloud."""
    from fitdays import FitdaysClient

    email = os.environ.get("FITDAYS_EMAIL")
    password = os.environ.get("FITDAYS_PASSWORD")

    if not email or not password:
        sys.exit(
            "FITDAYS_EMAIL and FITDAYS_PASSWORD must be configured."
        )

    print("Logging in to Fitdays...")

    async with await FitdaysClient.login(email, password) as client:
        result = await client.get_sync()

        print(f"Fitdays returned {len(result.measurements)} measurements.")

        profiles = {
            profile.suid: profile
            for profile in result.profiles
        }

        if profiles:
            print("Fitdays profiles:")

            for suid, profile in profiles.items():
                print(
                    f"  - {profile.display_name} "
                    f"(suid={suid})"
                )

        measurements = result.measurements

        if FITDAYS_PROFILE:
            selected_suids = {
                suid
                for suid, profile in profiles.items()
                if (
                    str(suid) == FITDAYS_PROFILE
                    or profile.display_name.lower()
                    == FITDAYS_PROFILE.lower()
                )
            }

            if not selected_suids:
                available = ", ".join(
                    f"{p.display_name} ({suid})"
                    for suid, p in profiles.items()
                )

                sys.exit(
                    f"FITDAYS_PROFILE '{FITDAYS_PROFILE}' not found. "
                    f"Available profiles: {available}"
                )

            measurements = [
                measurement
                for measurement in measurements
                if getattr(measurement, "suid", None)
                in selected_suids
            ]

        elif len(profiles) > 1:
            available = ", ".join(
                f"{p.display_name} ({suid})"
                for suid, p in profiles.items()
            )

            sys.exit(
                "Multiple Fitdays profiles found. Configure "
                f"FITDAYS_PROFILE. Available profiles: {available}"
            )

        by_date = {}
        skipped = 0

        for measurement in measurements:
            weight = value_or_none(measurement, "weight_kg")

            if weight is None:
                skipped += 1
                continue

            try:
                date = measurement_date(measurement)
            except ValueError as err:
                print(f"Skipping measurement: {err}")
                skipped += 1
                continue

            entry = {
                "date": date,
                "weight": weight,
                "percent_fat": value_or_none(
                    measurement,
                    "body_fat_pct",
                ),
                "muscle_mass": value_or_none(
                    measurement,
                    "muscle_mass_kg",
                ),
                "visceral_fat_rating": value_or_none(
                    measurement,
                    "visceral_fat",
                ),
                "percent_hydration": value_or_none(
                    measurement,
                    "body_water_pct",
                ),
                "basal_met": value_or_none(
                    measurement,
                    "bmr",
                ),
            }

            entry["filled"] = sum(
                value is not None
                for key, value in entry.items()
                if key != "filled"
            )

            previous = by_date.get(date)

            # If multiple measurements exist on the same day,
            # keep the most complete one.
            if (
                previous is None
                or entry["filled"] > previous["filled"]
            ):
                by_date[date] = entry

        rows = [by_date[date] for date in sorted(by_date)]

        return rows, skipped


def payload(entry):
    timestamp = f"{entry['date']}T{WEIGH_HOUR}"

    datetime.fromisoformat(timestamp)

    result = {
        "timestamp": timestamp,
        "weight": entry["weight"],
        "bmi": round(
            entry["weight"] / (HEIGHT_M ** 2),
            1,
        ),
    }

    optional_fields = (
        "percent_fat",
        "percent_hydration",
        "muscle_mass",
        "basal_met",
        "visceral_fat_rating",
    )

    for field in optional_fields:
        if entry[field] is not None:
            result[field] = entry[field]

    return result


def connect_garmin():
    """
    Resume the cached Garmin session or log in with credentials.
    """
    from garminconnect import Garmin

    api = Garmin()

    try:
        api.login(TOKENSTORE)
        print("Garmin session resumed from cached token.")
        return api
    except Exception as err:
        print(f"No usable cached Garmin session: {err}")

    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")

    if not email or not password:
        sys.exit(
            "GARMIN_EMAIL and GARMIN_PASSWORD must be configured."
        )

    api = Garmin(
        email,
        password,
        prompt_mfa=lambda: input("Garmin MFA code: ").strip(),
    )

    api.login(TOKENSTORE)

    print(f"Garmin login successful; token saved to {TOKENSTORE}")

    return api


def fetch_existing(api, first_date, last_date):
    """
    Return Garmin weigh-ins already present in the requested period.
    """
    existing = {}

    for year in range(
        int(first_date[:4]),
        int(last_date[:4]) + 1,
    ):
        start = max(f"{year}-01-01", first_date)
        end = min(f"{year}-12-31", last_date)

        try:
            data = api.get_body_composition(start, end)
        except Exception as err:
            print(
                f"  ! {year}: Garmin read failed ({err}); "
                "no entries will be skipped for this year."
            )
            continue

        for item in (data or {}).get("dateWeightList", []):
            date = item.get("calendarDate")
            grams = item.get("weight")

            if date:
                existing[date] = (
                    round(grams / 1000, 2)
                    if grams is not None
                    else None
                )

    return existing


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be uploaded",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="upload dates already present in Garmin",
    )

    args = parser.parse_args()

    rows, skipped = asyncio.run(load_fitdays())

    if not rows:
        sys.exit("No valid Fitdays measurements were found.")

    print(
        f"{len(rows)} unique Fitdays weigh-ins | "
        f"{skipped} measurements discarded"
    )

    print(
        f"Range: {rows[0]['date']} to "
        f"{rows[-1]['date']}\n"
    )

    api = connect_garmin()

    existing = {}

    if not args.force:
        print("Checking existing Garmin measurements...")

        existing = fetch_existing(
            api,
            rows[0]["date"],
            rows[-1]["date"],
        )

        print(
            f"{len(existing)} Garmin weigh-ins already "
            "present in this period.\n"
        )

    todo = []
    skipped_existing = []

    for entry in rows:
        if entry["date"] in existing:
            skipped_existing.append(
                (entry, existing[entry["date"]])
            )
        else:
            todo.append(entry)

    for entry, garmin_kg in skipped_existing:
        difference = ""

        if (
            garmin_kg is not None
            and abs(garmin_kg - entry["weight"]) >= 0.05
        ):
            difference = (
                f" (Fitdays {entry['weight']} kg, "
                f"Garmin {garmin_kg} kg)"
            )

        print(
            f"  = {entry['date']} already present"
            f"{difference}"
        )

    print(
        f"\nTo upload: {len(todo)} | "
        f"Skipped: {len(skipped_existing)}"
    )

    if args.dry_run:
        print()

        for entry in todo:
            print(payload(entry))

        print("\n[dry-run] Nothing uploaded.")
        return

    if not todo:
        print("Nothing to do.")
        return

    print()

    succeeded = 0
    failed = 0

    for entry in todo:
        try:
            api.add_body_composition(**payload(entry))

            succeeded += 1

            print(
                f"  OK {entry['date']} "
                f"{entry['weight']} kg"
            )

        except Exception as err:
            failed += 1

            print(
                f"  FAILED {entry['date']}: {err}"
            )

        time.sleep(SLEEP)

    result = f"\nUploaded {succeeded}/{len(todo)}"

    if failed:
        result += f"; {failed} failed"

    print(result)

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
