import json
from pathlib import Path
from storage import latest_rows, history_for

OUT = Path("data/latest.json")


def main():
    rows = latest_rows()
    for r in rows:
        nights = r.get("nights") or 0
        p2 = r.get("two_guest_interior_pp") or r.get("two_guest_pp_price")
        p1 = r.get("one_guest_interior") or r.get("one_guest_price")
        r["two_guest_pp_per_night"] = round(p2 / nights, 2) if p2 and nights else None
        r["one_guest_per_night"] = round(p1 / nights, 2) if p1 and nights else None
        r["solo_premium_pct"] = round((p1 / p2 - 1) * 100, 1) if p1 and p2 else None
        current = p2 if p2 is not None else p1
        prev = r.get("prev_price")
        r["price_change"] = round(current - prev, 2) if current is not None and prev is not None else None

        if r.get("line") == "Princess":
            r["fare_basis"] = "Princess Standard"
            r["plus_addon_per_night"] = 79
            r["premier_addon_per_night"] = 129
        else:
            r["fare_basis"] = "Royal Caribbean Fare"
            r["plus_addon_per_night"] = None
            r["premier_addon_per_night"] = None

        r["history"] = history_for(r["cruise_id"])

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"Wrote {len(rows)} cruises to {OUT}")


if __name__ == "__main__":
    main()
