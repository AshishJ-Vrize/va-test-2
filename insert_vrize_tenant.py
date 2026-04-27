import asyncio
import sys

# psycopg3 async requires SelectorEventLoop on Windows (ProactorEventLoop is the default)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

print("Step 1: importing modules...")
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.db.central.models import Tenant
from app.config.settings import get_settings
print("Step 2: modules imported")

async def insert():
    settings = get_settings()
    ms_tenant_id = settings.AZURE_TENANT_ID
    print(f"Step 3: ms_tenant_id = {ms_tenant_id!r}")

    print("Step 4: creating engine...")
    engine = create_async_engine(settings.CENTRAL_DB_URL, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    print("Step 5: opening session...")
    async with session_factory() as session:
        print("Step 6: session open, adding tenant...")
        tenant = Tenant(
            org_name="vrize",
            display_name="Vrize",
            ms_tenant_id=ms_tenant_id,
            db_host="va-central-db-dev.postgres.database.azure.com",
            db_region="centralindia",
            db_sku="Burstable, B2s, 2 vCores, 4 GiB RAM, 32 GiB storage",
            blob_container="vrize-data",
            status="active",
            plan="trial",
            max_users=100,
        )
        session.add(tenant)
        print("Step 7: committing...")
        await session.commit()
        print("Step 8: done!")

    await engine.dispose()

asyncio.run(insert())
print("Vrize tenant inserted.")
