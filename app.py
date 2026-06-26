from flask import Flask, render_template, request, jsonify, Response
import requests
import csv
import time
import io
import json
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)

# API key is read from environment variable on the server.
# Set GOOGLE_PLACES_API_KEY in Render's Environment settings.
SERVER_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")

# ============================================================
# GENERATE SEARCH POINTS AROUND A CITY
# ============================================================

def get_search_points(center_lat, center_lng, radius_meters):
    """
    Returns a list of (lat, lng) points covering the search area.
    Capped at 7 points max for speed. Each point covers 25km radius.
    """
    points = [(center_lat, center_lng)]
    R = 6371000
    sub_radius = 25000

    ring_distance = sub_radius * 1.2
    for i in range(6):
        angle = (2 * math.pi * i) / 6
        dlat = (ring_distance / R) * (180 / math.pi)
        dlng = dlat / math.cos(math.radians(center_lat))
        new_lat = center_lat + dlat * math.sin(angle)
        new_lng = center_lng + dlng * math.cos(angle)
        points.append((new_lat, new_lng))

    return points


# ============================================================
# RELEVANCE FILTER
# ============================================================

RELEVANT_KEYWORDS = [
    "tree", "arbor", "arborist", "stump", "trimming",
    "pruning", "land clearing", "landscap", "lawn",
    "contractor", "roofing", "plumb", "pest", "exterminat",
    "gutter", "fence", "concrete", "paving", "pressure wash",
    "handyman", "remodel", "construct", "demo"
]

EXCLUDE_KEYWORDS = [
    "hair", "salon", "barber", "beauty", "nail", "spa",
    "restaurant", "pizza", "taco", "burger", "sushi",
    "dental", "dentist", "doctor", "medical", "clinic",
    "school", "church", "daycare", "gym", "fitness",
    "hotel", "motel", "insurance", "lawyer", "attorney",
    "accounting", "tax", "real estate", "mortgage"
]


def is_relevant_business(name, types, search_query):
    name_lower = name.lower()
    types_str = " ".join(types).lower() if types else ""
    query_lower = search_query.lower()

    for kw in EXCLUDE_KEYWORDS:
        if kw in name_lower:
            return False

    query_words = query_lower.split()
    for word in query_words:
        if len(word) > 3 and word in name_lower:
            return True

    for kw in RELEVANT_KEYWORDS:
        if kw in name_lower:
            return True

    relevant_types = [
        "general_contractor", "roofing_contractor", "painter",
        "plumber", "electrician", "landscaper", "lawn_care",
        "tree_service", "pest_control", "moving_company"
    ]
    for t in relevant_types:
        if t in types_str:
            return True

    return False


# ============================================================
# GOOGLE PLACES FUNCTIONS
# ============================================================

def geocode_city(location, api_key):
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": location, "key": api_key}
    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if data.get("results"):
            loc = data["results"][0]["geometry"]["location"]
            return loc["lat"], loc["lng"]
    except Exception as e:
        print(f"Geocode error: {e}")
    return None, None


def search_one_point(lat, lng, keyword, api_key, sub_radius=25000, max_seconds=25):
    """
    Searches one point with pagination, but never runs longer than
    max_seconds total — prevents getting stuck on a slow next_page_token.
    """
    url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
    results = []
    params = {
        "location": f"{lat},{lng}",
        "radius": sub_radius,
        "keyword": keyword,
        "key": api_key
    }
    start_time = time.time()

    for page_num in range(3):
        if time.time() - start_time > max_seconds:
            print(f"Zone timeout after {max_seconds}s, returning {len(results)} results so far")
            break

        try:
            r = requests.get(url, params=params, timeout=8)
            data = r.json()
            status = data.get("status", "")

            if status == "ZERO_RESULTS":
                break
            if status not in ["OK", "ZERO_RESULTS"]:
                print(f"API status: {status} — {data.get('error_message', '')}")
                break

            results.extend(data.get("results", []))

            next_token = data.get("next_page_token")
            if not next_token:
                break

            if time.time() - start_time > max_seconds - 4:
                break

            time.sleep(2.5)
            params = {"pagetoken": next_token, "key": api_key}

        except Exception as e:
            print(f"Search error at point ({lat},{lng}): {e}")
            break

    return results


def search_full_city_gen(query, location, api_key, radius_meters):
    """
    Generator that yields SSE-ready progress dicts, then a final
    {'type': '_results', 'results': [...]} dict with all places found.
    Has an overall safety timeout so a slow zone never blocks forever.
    """
    seen_ids = set()
    all_results = []
    city_start_time = time.time()
    MAX_CITY_SECONDS = 180  # 3 minutes max per city search phase

    city_lat, city_lng = geocode_city(location, api_key)
    if not city_lat:
        yield {"type": "error", "message": f"Could not geocode: {location}"}
        yield {"type": "_results", "results": [], "city_lat": None, "city_lng": None}
        return

    yield {
        "type": "city_center",
        "lat": city_lat, "lng": city_lng,
        "city": location, "radius_meters": radius_meters
    }

    points = get_search_points(city_lat, city_lng, radius_meters)
    yield {"type": "status", "message": f"Scanning {len(points)} zones in {location}..."}

    for i, (lat, lng) in enumerate(points):
        if time.time() - city_start_time > MAX_CITY_SECONDS:
            yield {"type": "status", "message": f"Time limit reached for {location}, moving on with {len(all_results)} results"}
            break

        yield {"type": "status", "message": f"Zone {i+1}/{len(points)} — searching {location}..."}

        point_results = search_one_point(lat, lng, query, api_key, sub_radius=25000, max_seconds=25)

        new_count = 0
        for r in point_results:
            pid = r.get("place_id")
            if pid and pid not in seen_ids:
                seen_ids.add(pid)
                all_results.append(r)
                new_count += 1

        yield {
            "type": "zone_done",
            "message": f"Zone {i+1}/{len(points)}: {new_count} new businesses ({len(all_results)} total so far)"
        }
        time.sleep(0.3)

    yield {"type": "_results", "results": all_results, "city_lat": city_lat, "city_lng": city_lng}


def get_place_details(place_id, api_key):
    url = "https://maps.googleapis.com/maps/api/place/details/json"
    params = {
        "place_id": place_id,
        "fields": (
            "name,formatted_phone_number,website,"
            "rating,user_ratings_total,"
            "formatted_address,business_status,"
            "reviews"
        ),
        "key": api_key
    }
    try:
        r = requests.get(url, params=params, timeout=8)
        return r.json().get("result", {})
    except:
        return {}


def get_place_details_parallel(place_ids, api_key, max_workers=15):
    """
    Fetches Place Details for many place_ids in parallel.
    Returns: dict {place_id: details_dict}
    """
    results = {}

    def fetch_one(pid):
        return pid, get_place_details(pid, api_key)

    if not place_ids:
        return results

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(fetch_one, pid) for pid in place_ids]
        for future in as_completed(futures):
            pid, details = future.result()
            results[pid] = details

    return results


def has_recent_review(details, months=12):
    reviews = details.get("reviews", [])
    if not reviews:
        return False
    cutoff = time.time() - (months * 30 * 24 * 3600)
    for review in reviews:
        review_time = review.get("time", 0)
        if review_time >= cutoff:
            return True
    return False


def is_website_alive(url):
    if not url:
        return False
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=4, allow_redirects=True)
        return r.status_code < 400
    except:
        return False


def check_websites_parallel(place_website_pairs, max_workers=15):
    """
    Checks multiple websites in parallel using a thread pool.
    Input: list of (key, url) tuples.
    Returns: dict {key: True/False}
    """
    results = {}

    def check_one(pair):
        key, url = pair
        if not url:
            return key, False
        return key, is_website_alive(url)

    if not place_website_pairs:
        return results

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(check_one, pair) for pair in place_website_pairs]
        for future in as_completed(futures):
            key, alive = future.result()
            results[key] = alive

    return results


def calificar_lead(reviews, website, address, max_reviews, min_rating, rating, require_website, require_address):
    score = 0
    tags = []

    if require_website:
        if not website:
            return None, None
        if any(bad in website.lower() for bad in ["facebook", "instagram", "yelp", "google"]):
            return None, None
        score += 2
        tags.append("Has website")

    if require_address:
        if not address or address.strip() == "":
            return None, None
        tags.append("Physical address confirmed")

    if reviews <= 50:
        score += 3
        tags.append("11-50 reviews — Hot Lead")
    elif reviews <= 100:
        score += 2
        tags.append("51-100 reviews — Good Lead")
    elif reviews <= max_reviews:
        score += 1
        tags.append(f"101-{max_reviews} reviews — Good Lead")

    if score >= 5:
        calificacion = "LEAD CALIENTE"
    elif score >= 3:
        calificacion = "BUEN LEAD"
    else:
        calificacion = "LEAD TIBIO"

    return calificacion, ", ".join(tags)


# ============================================================
# ROUTES
# ============================================================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/scrape", methods=["POST"])
def scrape():
    data = request.json

    api_key = SERVER_API_KEY
    business_types_raw = data.get("business_types", "")
    locations_raw = data.get("locations", "")
    max_reviews = int(data.get("max_reviews", 200))
    min_rating = float(data.get("min_rating", 4.4))
    require_website = data.get("require_website", True)
    require_address = data.get("require_address", True)
    radius_miles = float(data.get("radius_miles", 60))
    radius_meters = int(radius_miles * 1609.34)

    if not api_key:
        return jsonify({"error": "Server is missing GOOGLE_PLACES_API_KEY. Contact admin."}), 400

    business_types = [b.strip() for b in business_types_raw.split(",") if b.strip()]
    locations = [l.strip() for l in locations_raw.split(",") if l.strip()]

    if not business_types or not locations:
        return jsonify({"error": "Business types and cities are required"}), 400

    def generate():
        all_leads = []
        seen_ids = set()

        total_combos = len(locations) * len(business_types)
        combo_num = 0
        overall_start = time.time()
        MAX_TOTAL_SECONDS = 25 * 60  # 25 minutes hard cap, under Render free's 30 min limit

        for location in locations:
            for business_type in business_types:
                if time.time() - overall_start > MAX_TOTAL_SECONDS:
                    yield f"data: {json.dumps({'type': 'status', 'message': 'Time limit reached (25 min). Stopping here to avoid server timeout.'})}\n\n"
                    break

                combo_num += 1
                yield f"data: {json.dumps({'type': 'status', 'message': f'[{combo_num}/{total_combos}] Searching: {business_type} in {location}...'})}\n\n"

                try:
                    # PHASE 1: collect all places with live zone-by-zone updates
                    places = []
                    for event in search_full_city_gen(business_type, location, api_key, radius_meters):
                        if event["type"] == "_results":
                            places = event["results"]
                        else:
                            yield f"data: {json.dumps(event)}\n\n"

                    yield f"data: {json.dumps({'type': 'status', 'message': f'Found {len(places)} businesses in {location} — pre-filtering...'})}\n\n"

                    # PHASE 2a: cheap filters first (no API calls needed) to shrink the list
                    pre_filtered = []
                    for place in places:
                        place_id = place.get("place_id")
                        if not place_id or place_id in seen_ids:
                            continue
                        seen_ids.add(place_id)

                        reviews = place.get("user_ratings_total", 0)
                        rating = place.get("rating", 0)

                        if reviews < 5 or reviews > max_reviews:
                            continue
                        if rating < min_rating and reviews > 0:
                            continue

                        pre_filtered.append(place)

                    # PHASE 2b: fetch Place Details for all survivors, in parallel
                    yield f"data: {json.dumps({'type': 'status', 'message': f'Fetching details for {len(pre_filtered)} businesses in parallel...'})}\n\n"

                    place_ids = [p.get("place_id") for p in pre_filtered]
                    details_map = get_place_details_parallel(place_ids, api_key, max_workers=15)

                    # PHASE 2c: apply detail-dependent filters
                    candidates = []
                    for place in pre_filtered:
                        place_id = place.get("place_id")
                        details = details_map.get(place_id, {})

                        name = details.get("name", place.get("name", "N/A"))
                        phone = details.get("formatted_phone_number", "")
                        website = details.get("website", "")
                        address = details.get("formatted_address", place.get("formatted_address", ""))
                        status = details.get("business_status", "OPERATIONAL")
                        reviews = place.get("user_ratings_total", 0)
                        rating = place.get("rating", 0)

                        if status != "OPERATIONAL":
                            continue
                        if not phone or phone.strip() == "":
                            continue

                        place_types = place.get("types", [])
                        if not is_relevant_business(name, place_types, business_type):
                            continue

                        if not has_recent_review(details):
                            continue

                        candidates.append({
                            "place_id": place_id,
                            "place": place,
                            "name": name,
                            "phone": phone,
                            "website": website,
                            "address": address,
                            "reviews": reviews,
                            "rating": rating,
                        })

                    # PHASE 3: check all websites in parallel — the slow part, done once
                    yield f"data: {json.dumps({'type': 'status', 'message': f'Checking {len(candidates)} websites in parallel...'})}\n\n"

                    website_pairs = [(c["place_id"], c["website"]) for c in candidates if c["website"]]
                    website_status = check_websites_parallel(website_pairs, max_workers=15)

                    # PHASE 4: build final leads
                    for c in candidates:
                        website = c["website"]

                        if website and not website_status.get(c["place_id"], False):
                            continue

                        calificacion, dolor = calificar_lead(
                            c["reviews"], website, c["address"],
                            max_reviews, min_rating, c["rating"],
                            require_website, require_address
                        )

                        if calificacion is None:
                            continue

                        place_lat = c["place"].get("geometry", {}).get("location", {}).get("lat", 0)
                        place_lng = c["place"].get("geometry", {}).get("location", {}).get("lng", 0)

                        lead = {
                            "Name": c["name"],
                            "Phone": c["phone"],
                            "Website": website,
                            "Address": c["address"],
                            "City": location,
                            "Type": business_type,
                            "Reviews": c["reviews"],
                            "Rating": c["rating"],
                            "Calificacion": calificacion,
                            "Tags": dolor,
                            "Status": "Pending call",
                            "Notes": "",
                            "lat": place_lat,
                            "lng": place_lng
                        }

                        all_leads.append(lead)
                        yield f"data: {json.dumps({'type': 'lead', 'lead': lead})}\n\n"

                except Exception as e:
                    yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

                time.sleep(0.3)

            if time.time() - overall_start > MAX_TOTAL_SECONDS:
                break

        yield f"data: {json.dumps({'type': 'done', 'total': len(all_leads)})}\n\n"

    return Response(generate(), mimetype="text/event-stream")


@app.route("/export", methods=["POST"])
def export_csv():
    leads = request.json.get("leads", [])
    if not leads:
        return jsonify({"error": "No leads to export"}), 400

    orden = {"LEAD CALIENTE": 0, "BUEN LEAD": 1, "LEAD TIBIO": 2}
    leads.sort(key=lambda x: orden.get(x.get("Calificacion", ""), 3))
    export_leads = [{k: v for k, v in l.items() if k not in ["lat", "lng"]} for l in leads]

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=export_leads[0].keys())
    writer.writeheader()
    writer.writerows(export_leads)

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=market_open_media_leads.csv"}
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
