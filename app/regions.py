from __future__ import annotations

import math
from typing import Any


DEFAULT_CITY = "moscow"


CITY_ALIASES: dict[str, str] = {
    "москва": "moscow",
    "msk": "moscow",
    "moscow": "moscow",
    "санкт-петербург": "spb",
    "санктпетербург": "spb",
    "питер": "spb",
    "спб": "spb",
    "spb": "spb",
    "saint-petersburg": "spb",
    "saintpetersburg": "spb",
    "екатеринбург": "ekb",
    "екб": "ekb",
    "ekb": "ekb",
    "yekaterinburg": "ekb",
    "новосибирск": "novosibirsk",
    "нск": "novosibirsk",
    "novosibirsk": "novosibirsk",
    "казань": "kazan",
    "kazan": "kazan",
    "нижний новгород": "nnov",
    "нижнийновгород": "nnov",
    "ннов": "nnov",
    "nnov": "nnov",
    "nizhny-novgorod": "nnov",
    "краснодар": "krasnodar",
    "krasnodar": "krasnodar",
    "ростов-на-дону": "rostov",
    "ростовнадону": "rostov",
    "ростов": "rostov",
    "rostov": "rostov",
    "rostov-on-don": "rostov",
    "челябинск": "chelyabinsk",
    "chelyabinsk": "chelyabinsk",
    "самара": "samara",
    "samara": "samara",
    "уфа": "ufa",
    "ufa": "ufa",
    "пермь": "perm",
    "perm": "perm",
    "омск": "omsk",
    "omsk": "omsk",
    "тюмень": "tyumen",
    "tyumen": "tyumen",
    "волгоград": "volgograd",
    "volgograd": "volgograd",
    "воронеж": "voronezh",
    "voronezh": "voronezh",
    "ярославль": "yaroslavl",
    "yaroslavl": "yaroslavl",
    "барнаул": "barnaul",
    "barnaul": "barnaul",
    "иркутск": "irkutsk",
    "irkutsk": "irkutsk",
    "калининград": "kaliningrad",
    "kaliningrad": "kaliningrad",
    "ижевск": "izhevsk",
    "izhevsk": "izhevsk",
    "кемерово": "kemerovo",
    "kemerovo": "kemerovo",
    "оренбург": "orenburg",
    "orenburg": "orenburg",
    "тольятти": "togliatti",
    "togliatti": "togliatti",
    "курск": "kursk",
    "kursk": "kursk",
    "набережные челны": "n_chelny",
    "набережныечелны": "n_chelny",
    "челны": "n_chelny",
    "n_chelny": "n_chelny",
}


REGIONS: dict[str, dict[str, Any]] = {
    "moscow": {
        "name": "Москва",
        "region_label": "RU/Moscow",
        "wb_dest": "1259570991",
        "ym_region_id": 213,
        "latitude": 55.7558,
        "longitude": 37.6173,
    },
    "spb": {
        "name": "Санкт-Петербург",
        "region_label": "RU/Saint-Petersburg",
        "wb_dest": "-1198054",
        "ym_region_id": 2,
        "latitude": 59.9311,
        "longitude": 30.3609,
    },
    "ekb": {
        "name": "Екатеринбург",
        "region_label": "RU/Yekaterinburg",
        "wb_dest": "-5818883",
        "ym_region_id": 54,
        "latitude": 56.8389,
        "longitude": 60.6057,
    },
    "novosibirsk": {
        "name": "Новосибирск",
        "region_label": "RU/Novosibirsk",
        "wb_dest": "-364764",
        "ym_region_id": 65,
        "latitude": 55.0084,
        "longitude": 82.9357,
    },
    "kazan": {
        "name": "Казань",
        "region_label": "RU/Kazan",
        "wb_dest": "-2133462",
        "ym_region_id": 43,
        "latitude": 55.7879,
        "longitude": 49.1233,
    },
    "nnov": {
        "name": "Нижний Новгород",
        "region_label": "RU/Nizhny-Novgorod",
        "wb_dest": "12358579",
        "ym_region_id": 47,
        "latitude": 56.2965,
        "longitude": 43.9361,
    },
    "krasnodar": {
        "name": "Краснодар",
        "region_label": "RU/Krasnodar",
        "wb_dest": "12358062",
        "ym_region_id": 35,
        "latitude": 45.0355,
        "longitude": 38.9753,
    },
    "rostov": {
        "name": "Ростов-на-Дону",
        "region_label": "RU/Rostov-on-Don",
        "wb_dest": "-2228342",
        "ym_region_id": 39,
        "latitude": 47.2357,
        "longitude": 39.7015,
    },
    "chelyabinsk": {
        "name": "Челябинск",
        "region_label": "RU/Chelyabinsk",
        "wb_dest": "-1581743",
        "ym_region_id": 56,
        "latitude": 55.1644,
        "longitude": 61.4368,
    },
    "samara": {
        "name": "Самара",
        "region_label": "RU/Samara",
        "wb_dest": "-283781",
        "ym_region_id": 51,
        "latitude": 53.1959,
        "longitude": 50.1002,
    },
    "ufa": {
        "name": "Уфа",
        "region_label": "RU/Ufa",
        "wb_dest": "12358495",
        "ym_region_id": 172,
        "latitude": 54.7388,
        "longitude": 55.9721,
    },
    "perm": {
        "name": "Пермь",
        "region_label": "RU/Perm",
        "wb_dest": "12358361",
        "ym_region_id": 50,
        "latitude": 58.0105,
        "longitude": 56.2502,
    },
    "omsk": {
        "name": "Омск",
        "region_label": "RU/Omsk",
        "wb_dest": "-3902444",
        "ym_region_id": 66,
        "latitude": 54.9885,
        "longitude": 73.3242,
    },
    "tyumen": {
        "name": "Тюмень",
        "region_label": "RU/Tyumen",
        "wb_dest": "12358470",
        "ym_region_id": 55,
        "latitude": 57.1530,
        "longitude": 65.5343,
    },
    "volgograd": {
        "name": "Волгоград",
        "region_label": "RU/Volgograd",
        "wb_dest": "-4039473",
        "ym_region_id": 38,
        "latitude": 48.7080,
        "longitude": 44.5133,
    },
    "voronezh": {
        "name": "Воронеж",
        "region_label": "RU/Voronezh",
        "wb_dest": "12358283",
        "ym_region_id": 193,
        "latitude": 51.6720,
        "longitude": 39.1843,
    },
    "yaroslavl": {
        "name": "Ярославль",
        "region_label": "RU/Yaroslavl",
        "wb_dest": "-3351953",
        "ym_region_id": 16,
        "latitude": 57.6261,
        "longitude": 39.8845,
    },
    "barnaul": {
        "name": "Барнаул",
        "region_label": "RU/Barnaul",
        "wb_dest": "-4024771",
        "ym_region_id": 197,
        "latitude": 53.3479,
        "longitude": 83.7798,
    },
    "irkutsk": {
        "name": "Иркутск",
        "region_label": "RU/Irkutsk",
        "wb_dest": "-5827722",
        "ym_region_id": 63,
        "latitude": 52.2862,
        "longitude": 104.2807,
    },
    "kaliningrad": {
        "name": "Калининград",
        "region_label": "RU/Kaliningrad",
        "wb_dest": "-331411",
        "ym_region_id": 22,
        "latitude": 54.7104,
        "longitude": 20.4522,
    },
    "izhevsk": {
        "name": "Ижевск",
        "region_label": "RU/Izhevsk",
        "wb_dest": "-971647",
        "ym_region_id": 44,
        "latitude": 56.8587,
        "longitude": 53.2415,
    },
    "kemerovo": {
        "name": "Кемерово",
        "region_label": "RU/Kemerovo",
        "wb_dest": "-1172839",
        "ym_region_id": 64,
        "latitude": 55.3535,
        "longitude": 86.0876,
    },
    "orenburg": {
        "name": "Оренбург",
        "region_label": "RU/Orenburg",
        "wb_dest": "12358336",
        "ym_region_id": 48,
        "latitude": 51.7727,
        "longitude": 55.0988,
    },
    "togliatti": {
        "name": "Тольятти",
        "region_label": "RU/Togliatti",
        "wb_dest": "-2448019",
        "ym_region_id": 240,
        "latitude": 53.5303,
        "longitude": 49.3461,
    },
    "kursk": {
        "name": "Курск",
        "region_label": "RU/Kursk",
        "wb_dest": "-2307160",
        "ym_region_id": 24,
        "latitude": 51.7373,
        "longitude": 36.1873,
    },
    "n_chelny": {
        "name": "Набережные Челны",
        "region_label": "RU/Naberezhnye-Chelny",
        "wb_dest": "-412732",
        "ym_region_id": 45,
        "latitude": 55.7942,
        "longitude": 52.4065,
    },
}


def resolve_city(value: str | None) -> tuple[str, dict[str, Any], list[str]]:
    diagnostics: list[str] = []
    if not value:
        return DEFAULT_CITY, REGIONS[DEFAULT_CITY], diagnostics

    key = value.strip().lower().replace("ё", "е")
    if key in REGIONS:
        return key, REGIONS[key], diagnostics

    canonical = CITY_ALIASES.get(key)
    if canonical and canonical in REGIONS:
        return canonical, REGIONS[canonical], diagnostics

    diagnostics.append(f"unknown_city={value!r}; fallback={DEFAULT_CITY}")
    return DEFAULT_CITY, REGIONS[DEFAULT_CITY], diagnostics


def resolve_location(
    value: str | None,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
) -> tuple[str, dict[str, Any], list[str]]:
    if latitude is None or longitude is None:
        return resolve_city(value)

    diagnostics: list[str] = []
    nearest_key = DEFAULT_CITY
    nearest_distance = float("inf")
    for key, info in REGIONS.items():
        distance = haversine_km(latitude, longitude, info["latitude"], info["longitude"])
        if distance < nearest_distance:
            nearest_key = key
            nearest_distance = distance

    info = dict(REGIONS[nearest_key])
    info["requested_latitude"] = latitude
    info["requested_longitude"] = longitude
    diagnostics.append(
        f"geo_nearest_city={nearest_key}; distance_km={nearest_distance:.1f}"
    )
    return nearest_key, info, diagnostics


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_km = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return radius_km * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def list_supported_regions() -> list[dict[str, Any]]:
    return [
        {
            "key": key,
            "name": info.get("name"),
            "latitude": info.get("latitude"),
            "longitude": info.get("longitude"),
        }
        for key, info in REGIONS.items()
    ]
