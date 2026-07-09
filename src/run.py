import uvicorn

from src.shared.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "src.main:app",
        host="0.0.0.0",  # noqa: S104 - listen on all interfaces inside the container
        port=settings.app_port,
        reload=settings.is_debug,
    )
