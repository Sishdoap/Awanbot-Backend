import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, BeforeValidator, Field, ConfigDict
from fastapi.responses import JSONResponse

from database import db, client
from dotenv import load_dotenv
from bson import ObjectId

load_dotenv()


# --- Database & Lifecycle ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        # Check if connection is successful
        await client.admin.command('ping')
        print("Pinged database successfully.")
    except Exception as e:
        print(f"Connection failed: {e}")
    yield


app = FastAPI(title="Awanbot", lifespan=lifespan)

# --- Security & Middleware ---
API_KEY = os.getenv("API_KEY")


async def verify_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API Key"
        )


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Models ---

# 1. Custom Type to handle MongoDB ObjectId automatically
PyObjectId = Annotated[str, BeforeValidator(str)]


# 2. Base Model for DB entries (adds 'id' field that maps to '_id')
class MongoBaseModel(BaseModel):
    id: Optional[PyObjectId] = Field(alias="_id", default=None)

    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True,
    )


# --- Feedback Models ---
class FeedbackCreate(BaseModel):
    name: str
    email: str
    feedback: str


# Inherits id handling from MongoBaseModel and fields from FeedbackCreate
class FeedbackInDB(MongoBaseModel, FeedbackCreate):
    time: datetime


# --- Booking Models ---
class BookingBase(BaseModel):
    name: str
    email: str
    start_time: datetime
    end_time: datetime
    topic: str


class BookingCreate(BookingBase):
    pass


class BookingInDB(MongoBaseModel, BookingBase):
    pass


# --- Routes ---

@app.get("/")
async def root():
    return {"message": "API is running."}


@app.post("/feedback", response_model=FeedbackInDB, dependencies=[Depends(verify_api_key)])
async def create_feedback(feedback_in: FeedbackCreate):
    # 1. Validation
    if len(feedback_in.feedback.strip()) < 10:
        raise HTTPException(status_code=400, detail="Feedback too short.")

    # 2. Prepare Data
    feedback_data = feedback_in.model_dump()
    feedback_data['time'] = datetime.now(timezone.utc)

    # 3. Insert
    result = await db["feedback"].insert_one(feedback_data)

    # 4. Return (PyObjectId will handle the conversion of _id)
    created_feedback = await db["feedback"].find_one({"_id": result.inserted_id})
    return created_feedback


@app.get("/feedback/{id}", response_model=FeedbackInDB, dependencies=[Depends(verify_api_key)])
async def get_feedback(id: str):
    if not ObjectId.is_valid(id):
        raise HTTPException(status_code=400, detail="Invalid ID format")

    doc = await db["feedback"].find_one({"_id": ObjectId(id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    return doc


# NOTE: response_model is now BookingInDB to include the ID
@app.post("/booking", response_model=BookingInDB, dependencies=[Depends(verify_api_key)])
async def create_booking(booking: BookingCreate):
    # 1. Logic Checks
    if booking.start_time >= booking.end_time:
        return JSONResponse(status_code=400,
                            content={"status": "failed", "reason": "Start time must be before end time."})

    if booking.start_time < datetime.now(timezone.utc):
        return JSONResponse(status_code=400,
                            content={"status": "failed", "reason": "Cannot create a booking in the past."})

    if booking.start_time.hour < 9 or booking.end_time.hour > 18:
        return JSONResponse(status_code=400,
                            content={"status": "failed", "reason": "Cannot create a booking outside office hours."})

    # 2. Check Overlaps
    overlap_query = {
        "start_time": {"$lt": booking.end_time},
        "end_time": {"$gt": booking.start_time}
    }

    existing_booking = await db["bookings"].find_one(overlap_query)

    if existing_booking:
        return JSONResponse(
            status_code=409,
            content={
                "status": "failed",
                "reason": f"Time slot unavailable. Overlaps with booking ID: {str(existing_booking['_id'])}",
            }
        )

    # 3. Insert
    new_booking = booking.model_dump()
    result = await db["bookings"].insert_one(new_booking)

    created_booking = await db["bookings"].find_one({"_id": result.inserted_id})
    return created_booking


@app.get("/booking/{id}", response_model=BookingInDB, dependencies=[Depends(verify_api_key)])
async def get_booking(id: str):
    if not ObjectId.is_valid(id):
        return JSONResponse(status_code=400, content={"status": "failed", "reason": "Invalid ID format"})

    doc = await db["bookings"].find_one({"_id": ObjectId(id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    return doc