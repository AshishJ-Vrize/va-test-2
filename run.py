import asyncio
import sys
from dotenv import load_dotenv

load_dotenv("C:/Users/Aakansha Sharma/Downloads/VA_Test/.env")

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn

config = uvicorn.Config("app.main:app", host="127.0.0.1", port=8000, log_level="debug")
server = uvicorn.Server(config)
asyncio.run(server.serve())

