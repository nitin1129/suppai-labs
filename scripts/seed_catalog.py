"""Load the shared test catalogue that every lab offers until the Tests & prices tab exists.

    python -m scripts.seed_catalog

Prices here are placeholders. Replace them with the list the member side uses today.
"""
import asyncio

from app.db import get_db

CATALOG = [
    {"_id": "THY-3", "name": "Thyroid Profile (T3, T4, TSH)", "parameters": 3, "price": 399, "mrp": 600,
     "fasting": False, "sample_type": "blood", "report_hours": 24, "home_collection": True},
    {"_id": "HBA1C", "name": "HbA1c", "parameters": 1, "price": 349, "mrp": 500,
     "fasting": False, "sample_type": "blood", "report_hours": 24, "home_collection": True},
    {"_id": "FBS", "name": "Fasting Blood Sugar", "parameters": 1, "price": 99, "mrp": 150,
     "fasting": True, "sample_type": "blood", "report_hours": 12, "home_collection": True},
    {"_id": "LIPID", "name": "Lipid Profile", "parameters": 8, "price": 499, "mrp": 800,
     "fasting": True, "sample_type": "blood", "report_hours": 24, "home_collection": True},
    {"_id": "DIAB-PKG", "name": "Diabetes Care Package", "parameters": 32, "price": 1299, "mrp": 2200,
     "fasting": True, "sample_type": "blood, urine", "report_hours": 36, "home_collection": True},
]


async def main() -> None:
    db = get_db()
    for t in CATALOG:
        await db.catalog_tests.update_one({"_id": t["_id"]}, {"$set": {**t, "active": True}}, upsert=True)
    print(f"{len(CATALOG)} tests loaded")


if __name__ == "__main__":
    asyncio.run(main())
