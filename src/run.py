import uvicorn

from src.shared.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "src.main:app",
        host="0.0.0.0",  # noqa: S104 - слушаем все интерфейсы внутри контейнера
        port=settings.app_port,
        reload=settings.is_debug,
    )
