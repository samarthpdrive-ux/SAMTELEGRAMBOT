import asyncio
import logging
from pprint import pprint

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update

from config import BOT_TOKEN


# ==========================
# USER ROUTERS
# ==========================

from handlers.start import (
    router as start_router
)

from handlers.products import (
    router as products_router
)

from handlers.orders import (
    router as orders_router
)

from handlers.deposit import (
    router as deposit_router
)

from handlers.referrals import (
    router as referrals_router
)

from handlers.support import (
    router as support_router
)


# ==========================
# ADMIN ROUTERS
# ==========================

from handlers.admin import (
    router as admin_router
)

from handlers.admin_products import (
    router as admin_products_router
)

from handlers.admin_product_manage import (
    router as admin_product_manage_router
)

from handlers.admin_orders import (
    router as admin_orders_router
)

from handlers.admin_deposits import (
    router as admin_deposits_router
)

from handlers.admin_support import (
    router as admin_support_router
)


# ==========================
# SERVICES
# ==========================

from services.deposit_checker import deposit_checker_loop

async def main():

    logging.basicConfig(
        level=logging.INFO
    )

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML
        )
    )

    dp = Dispatcher()

    # ==========================
    # DEBUG ALL UPDATES
    # ==========================

    @dp.update()
    async def debug_update(
            update: Update
    ):

        print(
            "\n========== UPDATE =========="
        )

        pprint(
            update.model_dump(
                exclude_none=True
            )
        )

        print(
            "============================\n"
        )

    # ==========================
    # LOAD ROUTERS
    # ==========================

    print("Loading routers...")

    # USER

    dp.include_router(
        start_router
    )

    dp.include_router(
        products_router
    )

    dp.include_router(
        orders_router
    )

    dp.include_router(
        referrals_router
    )

    dp.include_router(
        deposit_router
    )

    # ADMIN

    dp.include_router(
        admin_router
    )

    dp.include_router(
        admin_products_router
    )

    dp.include_router(
        admin_product_manage_router
    )

    dp.include_router(
        admin_orders_router
    )

    dp.include_router(
        admin_deposits_router
    )

    dp.include_router(
        admin_support_router
    )

    # SUPPORT MUST BE LAST

    dp.include_router(
        support_router
    )

    print(
        "✅ Routers Loaded"
    )

    # ==========================
    # START BACKGROUND TASKS
    # ==========================

    asyncio.create_task(
        deposit_checker_loop()
    )

    print(
        "✅ Deposit checker started"
    )

    me = await bot.get_me()

    print(
        f"✅ Logged in as "
        f"@{me.username}"
    )

    # ==========================
    # START BOT
    # ==========================

    await dp.start_polling(
        bot
    )


if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        print(
            "Bot stopped."
        )

    except Exception as e:

        print(
            "FATAL ERROR:"
        )

        print(e)