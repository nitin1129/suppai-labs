"""Create or reset an admin user.

    python -m scripts.create_admin admin@suppai.in
"""
import asyncio
import getpass
import sys

from app.db import get_db
from app.security import hash_password, now, password_problems
from app.services.common import new_id


async def main(email: str) -> None:
    password = getpass.getpass("Password: ")
    if problems := password_problems(password):
        sys.exit("Password needs " + ", ".join(problems))
    db = get_db()
    await db.admin_users.update_one(
        {"email": email.lower()},
        {"$set": {"password_hash": hash_password(password), "updated_at": now()},
         "$setOnInsert": {"_id": new_id(), "created_at": now()}},
        upsert=True,
    )
    print(f"Admin {email} saved")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1]))
