from .auth    import router as auth_router
from .vps     import router as vps_router
from .nodes   import router as nodes_router
from .admin   import router as admin_router
from .billing import router as billing_router
from .console import router as console_router
from .account import router as account_router
from .tickets import router as tickets_router

__all__ = [
    "auth_router", "vps_router", "nodes_router",
    "admin_router", "billing_router", "console_router", "account_router",
    "tickets_router",
]
