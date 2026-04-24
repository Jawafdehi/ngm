#!/usr/bin/env python
"""Script to run the NGM Court Case API server."""

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "ngm.api.app:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )
