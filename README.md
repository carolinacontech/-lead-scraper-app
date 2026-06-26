# Market Open Media — Lead Scraper

Internal tool for finding qualified sales leads from Google Maps. Built for Market Open Media's outbound sales process targeting home services businesses (tree removal, landscaping, contractors, and similar trades).

## What it does

- Searches any city or region for businesses matching custom keywords
- Covers wide search areas using a multi-zone radius search (beyond Google's default 60-result cap)
- Filters leads by:
  - Minimum/maximum review count
  - Minimum star rating
  - Valid, live website (checked in real time)
  - Physical address on file
  - Phone number on file
  - At least one review in the past 12 months
  - Relevance match against business type (filters out unrelated results like salons or restaurants)
- Displays results live on an interactive map and as scrollable lead cards
- One-click discard for irrelevant leads
- Click-to-call integration with JustCall
- CSV export, sorted by lead quality

## Stack

- **Backend:** Flask (Python), Google Places API, Google Geocoding API
- **Frontend:** Vanilla HTML/JS, Leaflet.js for mapping
- **Hosting:** Render.com
- **Performance:** Parallelized website-liveness checks via `ThreadPoolExecutor`

## Setup

1. Clone the repo
2. `pip install -r requirements.txt`
3. Set the `GOOGLE_PLACES_API_KEY` environment variable (Places API + Geocoding API must be enabled on the Google Cloud project)
4. `python app.py`
5. Visit `http://localhost:5000`

## Status

Active internal tool — not for public/customer use.
