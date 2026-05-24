from __future__ import annotations

from dataclasses import dataclass
from typing import Any


REGION_LABEL_TO_CITY: dict[str, str] = {
    "RU/Moscow": "Москва",
    "RU/Saint Petersburg": "Санкт-Петербург",
    "RU/SaintPetersburg": "Санкт-Петербург",
    "RU/Yekaterinburg": "Екатеринбург",
    "RU/Kazan": "Казань",
    "RU/Novosibirsk": "Новосибирск",
    "RU/Krasnodar": "Краснодар",
    "RU/Nizhny Novgorod": "Нижний Новгород",
    "RU/Rostov-on-Don": "Ростов-на-Дону",
    "RU/Chelyabinsk": "Челябинск",
    "RU/Samara": "Самара",
    "RU/Ufa": "Уфа",
    "RU/Voronezh": "Воронеж",
}


@dataclass
class RegionContext:
    canonical_label: str
    city_name_ru: str
    requested_label: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical": self.canonical_label,
            "city": self.city_name_ru,
            "requested": self.requested_label,
        }


def build_region_context(region_label: str | None) -> RegionContext:
    requested = (region_label or "RU/Moscow").strip()
    city = REGION_LABEL_TO_CITY.get(requested)
    if not city:
        if "/" in requested:
            city = requested.split("/", 1)[1].strip()
        else:
            city = requested
    return RegionContext(canonical_label=requested, city_name_ru=city, requested_label=requested)


def per_domain_cookies(region: RegionContext) -> dict[str, dict[str, str]]:
    base_city_ru = region.city_name_ru
    return {
        "*": {},
        "dns-shop.ru": {"city_path": _ascii_slug(base_city_ru)},
        "citilink.ru": {"_space": _ascii_slug(base_city_ru)},
        "mvideo.ru": {"MVID_CITY_ID": "", "MVID_REGION_ID": ""},
    }


def is_region_supported(domain: str) -> bool:
    return False


def _ascii_slug(value: str) -> str:
    mapping = {
        "москва": "moscow",
        "санкт-петербург": "spb",
        "екатеринбург": "ekaterinburg",
        "казань": "kazan",
        "новосибирск": "novosibirsk",
        "краснодар": "krasnodar",
        "нижний новгород": "nnovgorod",
        "ростов-на-дону": "rostov",
        "челябинск": "chelyabinsk",
        "самара": "samara",
        "уфа": "ufa",
        "воронеж": "voronezh",
    }
    return mapping.get(value.lower(), "moscow")
