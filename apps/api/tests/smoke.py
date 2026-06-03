from sqlalchemy import create_engine, text

from apps.api.app.bootstrap import seed
from apps.api.app.config import get_settings


def main() -> None:
    settings = get_settings()
    seed()
    engine = create_engine(settings.database_url, future=True)
    with engine.connect() as connection:
        project_count = connection.execute(text("SELECT COUNT(*) FROM projects")).scalar_one()
    if project_count < 1:
        raise SystemExit("No seeded project found.")
    print("Smoke check passed.")


if __name__ == "__main__":
    main()
