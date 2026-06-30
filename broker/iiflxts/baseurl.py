"""IIFL XTS (Symphony) broker base URLs configuration."""

# Base URL for IIFL XTS API endpoints (Symphony XTS, IIFL Securities)
BASE_URL = "https://ttblaze.iifl.com"

# Derived URLs for specific API endpoints
MARKET_DATA_URL = f"{BASE_URL}/apimarketdata"
INTERACTIVE_URL = f"{BASE_URL}/interactive"
