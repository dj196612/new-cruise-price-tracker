# Cruise Price Tracker

Tracks Princess, Royal Caribbean, Celebrity, Carnival and NCL sailings from Australian ports
(1-guest solo and 2-guest fares). Each line has its own collector in `collectors/` and its own
GitHub Actions workflow; all share the same database and dashboard.

Solo fares for Royal Caribbean and Celebrity come from `collectors/rcg_solo_graph.py` (the site's own
AU cruise-search GraphQL replayed at 1 adult), with the itinerary API as a fallback.

## What it tracks

- Princess cruises departing Sydney, Brisbane and Melbourne
- Departure date
- Number of nights
- Ship
- Itinerary / route text
- 1 guest fare
- 2 guest per-person fare
- 2 guest total fare
- Solo premium %
- Price per night
- Availability
- Current price
- Previous price
- Lowest recorded price
- Price movement
- Direct Princess URL

The database stores every observation instead of overwriting old fares.

## First run

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
python run_tracker.py
```

Then serve the dashboard:

```bash
python -m http.server 8000
```

Open:

http://localhost:8000/website/

## Test the dashboard before scraping

The project includes sample Princess data in `data/latest.json`, so the dashboard works immediately.

## Princess website changes

Princess uses a dynamic search interface. `collectors/princess.py` contains the Princess-specific
browser logic and conservative text-based selectors. If Princess changes its page, repair that file only.

The collector saves a debug screenshot and HTML snapshot in `debug/` if it cannot find cruise cards.

## Adding the next cruise line

Create another collector that returns records in the same schema:

```text
line
cruise_id
name
ship
departure_port
arrival_port
departure_date
nights
itinerary
url
available
one_guest_price
two_guest_pp_price
two_guest_total
currency
```

Then pass those records to the same database functions in `storage.py`.

## GitHub Actions

A workflow is included to run at approximately 6am and 6pm Australia/Sydney time, update the database
and JSON, then commit changed tracker data back to the repository.

Before relying on it, run the Princess collector locally once and check `debug/` if Princess has changed
its UI or bot protection.
