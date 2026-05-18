from aiogram import Router
from handlers.orders import router as orders_router
from handlers.dashboard import router as dashboard_router

router = Router()
router.include_router(orders_router)
router.include_router(dashboard_router)

__all__ = ["router"]

