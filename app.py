from flask import Flask, render_template, request, jsonify, Response
import requests
import csv
import time
import io
import json
import math

app = Flask(__name__)

# ============================================================
# GENERATE SEARCH POINTS AROUND A CITY
# ============================================================

def get_search_points(center_lat, center_lng, radius_meters):
    """
    Returns a list of (lat, lng) points covering the search area.
    Capped at 7 points max for speed. Each point covers 25km radius.
    """
    points = [(center_lat, center_lng)]  # center point
    R = 6371000
    sub_radius = 25000  # 25km per point — wider coverage, fewer points

    # Only 1 ring of 6 points around the center = 7 total max
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
# GOOGLE PLACES FUNCTIONS
# ============================================================

# Keywords that must appear in business name or types
RELEVANT_KEYWORDS = [
    "tree", "arbor", "arborist", "stump", "trimming",
    "pruning", "land clearing", "landscap", "lawn",
    "contractor", "roofing", "plumb", "pest", "exterminat",
    "gutter", "fence", "concrete", "paving", "pressure wash",
    "handyman", "remodel", "construct", "demo"
]

# Keywords that immediately discard a business
EXCLUDE_KEYWORDS = [
    "hair", "salon", "barber", "beauty", "nail", "spa",
    "restaurant", "pizza", "taco", "burger", "sushi",
    "dental", "dentist", "doctor", "medical", "clinic",
    "school", "church", "daycare", "gym", "fitness",
    "hotel", "motel", "insurance", "lawyer", "attorney",
    "accounting", "tax", "real estate", "mortgage"
]


def is_relevant_business(name, types, search_query):
    """
    Returns True if the business is relevant to the search.
    Checks name and Google place types against keyword lists.
    """
    name_lower = name.lower()
    types_str = " ".join(types).lower() if types else ""
    query_lower = search_query.lower()

    # Immediately discard if name contains exclude keywords
    for kw in EXCLUDE_KEYWORDS:
        if kw in name_lower:
            return False

    # Check if name contains the search query words
    query_words = query_lower.split()
    for word in query_words:
        if len(word) > 3 and word in name_lower:
            return True

    # Check if name contains any relevant keyword
    for kw in RELEVANT_KEYWORDS:
        if kw in name_lower:
            return True

    # Check Google place types
    relevant_types = [
        "general_contractor", "roofing_contractor", "painter",
        "plumber", "electrician", "landscaper", "lawn_care",
        "tree_service", "pest_control", "moving_company"
    ]
    for t in relevant_types:
        if t in types_str:
            return True

    return False


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


def search_one_point(lat, lng, keyword, api_key, sub_radius=15000):
    """
    Search a single point with full pagination.
    Returns up to 60 results (3 pages x 20).
    """
    url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
    results = []
    params = {
        "location": f"{lat},{lng}",
        "radius": sub_radius,
        "keyword": keyword,
        "key": api_key
    }

    for page_num in range(3):
        try:
            r = requests.get(url, params=params, timeout=10)
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

            # Google requires 2-3 second delay before next page
            time.sleep(3)
            params = {"pagetoken": next_token, "key": api_key}

        except Exception as e:
            print(f"Search error at point ({lat},{lng}): {e}")
            break

    return results


def search_full_city(query, location, api_key, radius_meters):
    """
    Searches across all grid points for a city.
    Deduplicates results across all points.
    """
    seen_ids = set()
    all_results = []

    # Get city center
    city_lat, city_lng = geocode_city(location, api_key)
    if not city_lat:
        print(f"Could not geocode: {location}")
        return [], None, None

    # Generate all search points
    points = get_search_points(city_lat, city_lng, radius_meters)
    print(f"Searching {len(points)} points in {location}")

    for i, (lat, lng) in enumerate(points):
        point_results = search_one_point(lat, lng, query, api_key, sub_radius=25000)

        new_count = 0
        for r in point_results:
            pid = r.get("place_id")
            if pid and pid not in seen_ids:
                seen_ids.add(pid)
                all_results.append(r)
                new_count += 1

        print(f"  Point {i+1}/{len(points)}: {len(point_results)} found, {new_count} new")
        time.sleep(0.5)

    return all_results, city_lat, city_lng


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
        r = requests.get(url, params=params, timeout=10)
        return r.json().get("result", {})
    except:
        return {}


def has_recent_review(details, months=12):
    """
    Returns True if the business has at least one review
    from the last 12 months. Uses the review timestamps
    returned by Places Details API.
    """
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
    """
    Checks if a website is actually accessible.
    Returns True if the site loads, False if it errors or times out.
    """
    if not url:
        return False
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=6, allow_redirects=True)
        # Accept any 2xx or 3xx response
        if r.status_code < 400:
            return True
        return False
    except:
        return False


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

    api_key = data.get("api_key", "").strip()
    business_types_raw = data.get("business_types", "")
    locations_raw = data.get("locations", "")
    max_reviews = int(data.get("max_reviews", 200))
    min_rating = float(data.get("min_rating", 4.4))
    require_website = data.get("require_website", True)
    require_address = data.get("require_address", True)
    radius_miles = float(data.get("radius_miles", 60))
    radius_meters = int(radius_miles * 1609.34)

    if not api_key:
        return jsonify({"error": "API Key required"}), 400

    business_types = [b.strip() for b in business_types_raw.split(",") if b.strip()]
    locations = [l.strip() for l in locations_raw.split(",") if l.strip()]

    if not business_types or not locations:
        return jsonify({"error": "Business types and cities are required"}), 400

    def generate():
        all_leads = []
        seen_ids = set()

        for location in locations:
            for business_type in business_types:
                yield f"data: {json.dumps({'type': 'status', 'message': f'Searching: {business_type} in {location}...'})}\n\n"

                try:
                    places, city_lat, city_lng = search_full_city(business_type, location, api_key, radius_meters)

                    if city_lat and city_lng:
                        yield f"data: {json.dumps({'type': 'city_center', 'lat': city_lat, 'lng': city_lng, 'city': location, 'radius_meters': radius_meters})}\n\n"

                    yield f"data: {json.dumps({'type': 'status', 'message': f'Found {len(places)} businesses in {location} — filtering...'})}\n\n"

                    for place in places:
                        place_id = place.get("place_id")
                        if not place_id or place_id in seen_ids:
                            continue
                        seen_ids.add(place_id)

                        reviews = place.get("user_ratings_total", 0)
                        rating = place.get("rating", 0)

                        # Minimum 5 reviews required
                        if reviews < 5:
                            continue
                        if reviews > max_reviews:
                            continue
                        if rating < min_rating and reviews > 0:
                            continue

                        details = get_place_details(place_id, api_key)
                        time.sleep(0.1)

                        name = details.get("name", place.get("name", "N/A"))
                        phone = details.get("formatted_phone_number", "")
                        website = details.get("website", "")
                        address = details.get("formatted_address", place.get("formatted_address", ""))
                        status = details.get("business_status", "OPERATIONAL")

                        if status != "OPERATIONAL":
                            continue

                        # Phone is mandatory
                        if not phone or phone.strip() == "":
                            continue

                        # Filter out irrelevant businesses
                        place_types = place.get("types", [])
                        if not is_relevant_business(name, place_types, business_type):
                            continue

                        # Verify website is actually accessible
                        if website and not is_website_alive(website):
                            continue

                        # Must have at least one review in the last 12 months
                        if not has_recent_review(details):
                            continue

                        calificacion, dolor = calificar_lead(
                            reviews, website, address,
                            max_reviews, min_rating, rating,
                            require_website, require_address
                        )

                        if calificacion is None:
                            continue

                        place_lat = place.get("geometry", {}).get("location", {}).get("lat", 0)
                        place_lng = place.get("geometry", {}).get("location", {}).get("lng", 0)

                        lead = {
                            "Name": name,
                            "Phone": phone,
                            "Website": website,
                            "Address": address,
                            "City": location,
                            "Type": business_type,
                            "Reviews": reviews,
                            "Rating": rating,
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

                time.sleep(0.5)

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
