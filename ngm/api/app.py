"""FastAPI application for NGM court case API."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from ngm.api.routes import router

app = FastAPI(
    title="NGM Court Case API",
    description="API for accessing Nepal court case data from the NGM database",
    version="1.0.0",
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routes
app.include_router(router)


@app.get("/", tags=["health"])
def health_check():
    """Health check endpoint."""
    return {"status": "ok", "service": "NGM Court Case API"}
