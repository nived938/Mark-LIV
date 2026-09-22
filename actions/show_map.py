# show_map.py
"""Open Google Maps inside the JARVIS HUD.

This action uses Google Maps URLs by default, so it works without a Google Maps
Platform API key. If config/api_keys.json contains google_maps_api_key, the UI
uses the Maps Embed API instead.
"""
from __future__ import annotations


def show_map(parameters: dict, player=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "open") or "open").strip().lower()
    location = " ".join(str(params.get("location", "") or "").split()).strip()

    if player is None:
        return "The HUD map interface is unavailable."

    if action in {"close", "hide", "dismiss"}:
        try:
            player.close_map()
        except Exception as exc:
            return f"Could not close the HUD map: {exc}"
        return "HUD map closed."

    if not location:
        return "Please provide a location to show on the HUD map."

    try:
        player.show_map(location)
    except Exception as exc:
        return f"Could not open the HUD map: {exc}"

    return f"Opened the map for {location} in the HUD."


TOOL = {
    "name": "show_map",
    "description": (
        "Opens an interactive Google Maps view inside the JARVIS HUD for a "
        "location, place, city, address, landmark, or region. Use this when the "
        "user says things like 'look at Kasaragod', 'show me Kasaragod on the "
        "map', 'open the map for London', 'where is the Eiffel Tower', or similar. "
        "Use action='close' when the user asks to close or hide the map. "
        "Do not use web_search for a simple map-location request."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "open (default) or close",
            },
            "location": {
                "type": "STRING",
                "description": "Location to display, for example 'Kasaragod, Kerala, India'",
            },
        },
        "required": [],
    },
    "handler": show_map,
}
