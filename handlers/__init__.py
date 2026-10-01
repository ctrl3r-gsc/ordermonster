from aiogram import Router
from handlers.assembly import router as assembly_router
from handlers.orders import router as orders_router
from handlers.dashboard import router as dashboard_router
from handlers.statistics import router as statistics_router
from handlers.notifications import router as notifications_router
from handlers.packing import router as packing_router

router = Router()
router.include_router(assembly_router)
router.include_router(notifications_router)
router.include_router(orders_router)
router.include_router(dashboard_router)
router.include_router(packing_router)
router.include_router(statistics_router)

__all__ = ["router"]
