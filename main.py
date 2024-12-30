from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from mongodb_manager import MongoDBManager
from routers import router


mongodb: MongoDBManager = None
from config import MONGODB_URL, MONGODB_DB_NAME

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize MongoDB connection when the app starts
    global mongodb
    mongodb = MongoDBManager(MONGODB_URL)
    print(f"Connected to MongoDB at {MONGODB_URL}")

    yield  # The application is running

    # Close MongoDB connection when the app stops
    if mongodb:
        mongodb.close()
        print("Closed MongoDB connection")


app = FastAPI()

app.include_router(router)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)

if __name__ == "__main__":

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
