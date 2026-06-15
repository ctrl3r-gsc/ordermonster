from aiogram import Router
from handlers.orders import router as orders_router
from handlers.dashboard import router as dashboard_router
from handlers.statistics import router as statistics_router
from handlers.notifications import router as notifications_router

router = Router()
router.include_router(notifications_router)
router.include_router(orders_router)
router.include_router(dashboard_router)
router.include_router(statistics_router)

__all__ = ["router"]
