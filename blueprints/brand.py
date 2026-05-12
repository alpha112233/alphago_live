# blueprints/brand.py
"""
Public brand endpoint — returns the product name + tagline shown in the UI.

Unauthenticated by design: the login and setup pages need to know the brand
before a user has signed in. There are no secrets here.

alphago_live fork addition. Upstream OpenAlgo hardcodes "OpenAlgo" in
~100 places across React templates. This endpoint + the BrandStore on the
frontend lets us override it from the BRAND_NAME / BRAND_TAGLINE env vars
without rebuilding the image.
"""

from flask import Blueprint, jsonify

from utils.config import get_brand_name, get_brand_tagline

brand_bp = Blueprint("brand_bp", __name__, url_prefix="/api")


@brand_bp.route("/brand", methods=["GET"])
def get_brand():
    return jsonify({
        "name": get_brand_name(),
        "tagline": get_brand_tagline(),
    })
