# Fitdays → Garmin Connect

Move your smart-scale history (weight + body composition) from the **Fitdays**
app into **Garmin Connect**, so it shows up in Garmin's weight/body-composition
graphs alongside your activities.

Garmin has no public API for uploading weight, so this uses the community
[`garminconnect`](https://github.com/cyberjunky/python-garminconnect) library
to log in as you and push each weigh-in.

What gets transferred per weigh-in:

| CSV column       | Garmin field           |
|------------------|------------------------|
| `weight_kg`      | weight                 |
| *(computed)*     | BMI (from `HEIGHT_M`)  |
| `body_fat_pct`   | body fat %             |
| `body_water_pct` | hydration %            |
| `muscle_mass_kg` | muscle mass            |
| `visceral_fat`   | visceral fat rating    |
| `bmr_kcal`       | basal metabolic rate   |

## 1. 


## 2. Install

Python 3.10+.

```bash
git clone https://github.com/rignaneseleo/fitdays-to-garmin.git
cd fitdays-to-garmin
pip install -r requirements.txt
```

## 3. Configure

```bash
export GARMIN_EMAIL='you@example.com'
export GARMIN_PASSWORD='your-password'   # single quotes so $ ! ` are not mangled by the shell
export HEIGHT_M=1.70                     # your height in metres, for BMI (default 1.70)
```

## 4. Dry run, then upload

```bash
python garmin_import.py scalehistory.csv --dry-run
```

The first run logs in (you will be asked for the Garmin MFA code if your
account has 2FA), then caches the session token in `~/.garminconnect`. Later
runs need neither credentials nor a code.

The dry run lists every payload that *would* be sent and which dates are
skipped because Garmin already has a weigh-in that day. When it looks right:

```bash
python garmin_import.py scalehistory.csv
```

The script is idempotent: re-running it only uploads dates that are missing on
Garmin. Use `--force` to upload even dates that already exist (creates a
second entry for that day).

Each weigh-in is stamped at 07:00 local time (the CSV has no time of day).
Change `WEIGH_HOUR` in the script if you prefer another time.

## Troubleshooting

**`429` / `Cloudflare` on every login strategy** — Garmin rate-limits the IP
after a handful of login attempts. Stop retrying for ~1 hour (each retry
resets the clock), or switch network (phone hotspot). Once the token is cached
this never happens again because no further logins are made.

**`401 Unauthorized (Invalid Username or Password)`** — check what the shell
actually exported:

```bash
python3 -c 'import os;print(os.environ["GARMIN_EMAIL"], len(os.environ["GARMIN_PASSWORD"]))'
```

A wrong length usually means the password contained a shell-special character
and was exported with double quotes.

**`MFA Required but no prompt_mfa mechanism supplied`** — you are running an
old version of this script; update.

**Want to start over?** Delete `~/.garminconnect` to force a fresh login.

## Notes

- Uses unofficial Garmin endpoints via `garminconnect`; Garmin can change them
  at any time.
- Your credentials never leave your machine; the token file in
  `~/.garminconnect` is equivalent to a logged-in session — keep it private.
- Not affiliated with Fitdays, ICOMON or Garmin.

## License

MIT
