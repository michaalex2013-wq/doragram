"""Бот: регистрация + оплата заказов (Telegram Stars; рубли — если задан PROVIDER_TOKEN).
Запуск: BOT_TOKEN=... python bot.py   (сначала запустите API — он создаёт таблицы)
ENV: BOT_TOKEN, PROVIDER_TOKEN (необязательно), PAY_CONTACT (контакт для ручной оплаты, по умолчанию @bogarendi)"""
import os
import secrets

from sqlalchemy import select
from telegram import LabeledPrice, Update
from telegram.ext import (ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler,
                          PreCheckoutQueryHandler, filters)

from database import Order, Product, SessionLocal, User, fulfill

PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN", "")
PAY_CONTACT = os.getenv("PAY_CONTACT", "@bogarendi")


async def get_user(tg) -> User:
    async with SessionLocal() as s:
        u = await s.scalar(select(User).where(User.tg_id == tg.id))
        if not u:
            u = User(tg_id=tg.id, phone=secrets.token_hex(6), login_code=secrets.token_hex(4).upper(),
                     name=(tg.first_name or "User")[:64])
            s.add(u)
            await s.flush()
            u.phone = f"+999{10_000_000 + u.id}"  # временный виртуальный номер
            await s.commit()
        return u


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = await get_user(update.effective_user)
    if u.is_banned:
        await update.message.reply_text("Доступ заблокирован.")
        return
    if ctx.args and ctx.args[0].startswith("pay_") and ctx.args[0][4:].isdigit():
        await pay(update, u, int(ctx.args[0][4:]))
        return
    await update.message.reply_text(
        f"Ваш виртуальный номер: {u.phone}\nКод для входа: {u.login_code}\n\n"
        "Введите код в приложении DoraGram. /newcode — выпустить новый код.")


async def newcode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = await get_user(update.effective_user)
    if u.is_banned:
        return
    async with SessionLocal() as s:
        row = await s.get(User, u.id)
        row.login_code = secrets.token_hex(4).upper()
        await s.commit()
    await update.message.reply_text(f"Новый код: {row.login_code}\nСтарый код больше не действует.")


async def pay(update: Update, u: User, oid: int):
    async with SessionLocal() as s:
        o = await s.get(Order, oid)
        p = await s.get(Product, o.product_id) if o else None
    m = update.message
    if not o or o.user_id != u.id or o.status != "pending":
        await m.reply_text("Заказ не найден или уже оплачен.")
        return
    title = p.title[:32]
    await m.reply_invoice(title=title, description=f"Заказ #{oid} в DoraGram", payload=f"order:{oid}",
                          currency="XTR", prices=[LabeledPrice(title, p.price_xtr)], provider_token="")
    if PROVIDER_TOKEN and p.price_rub > 0:
        await m.reply_invoice(title=title, description=f"Заказ #{oid} в DoraGram", payload=f"order:{oid}",
                              currency="RUB", prices=[LabeledPrice(title, p.price_rub * 100)],
                              provider_token=PROVIDER_TOKEN)
    else:
        await m.reply_text(f"Оплата в рублях ({p.price_rub} ₽): напишите {PAY_CONTACT} и укажите заказ #{oid}. "
                           "Товар выдадут после подтверждения оплаты.")


def order_id(payload: str) -> int:
    return int(payload.split(":")[1]) if payload.startswith("order:") else 0


async def precheckout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.pre_checkout_query
    try:
        oid = order_id(q.invoice_payload)
    except ValueError:
        oid = 0
    async with SessionLocal() as s:
        o = await s.get(Order, oid)
        p = await s.get(Product, o.product_id) if o else None
        u = await s.scalar(select(User).where(User.tg_id == q.from_user.id))
    expected = (p.price_xtr if q.currency == "XTR" else p.price_rub * 100) if p else -1
    ok = bool(o and u and o.user_id == u.id and o.status == "pending" and q.total_amount == expected)
    await q.answer(ok=ok, error_message=None if ok else "Заказ недоступен или цена изменилась")


async def paid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sp = update.message.successful_payment
    oid = order_id(sp.invoice_payload)
    r = await fulfill(oid, "xtr" if sp.currency == "XTR" else "rub", sp.total_amount,
                      sp.telegram_payment_charge_id)
    await update.message.reply_text({
        "ok": "Оплата получена, товар выдан. Откройте приложение DoraGram.",
        "already": "Этот заказ уже обработан.",
        "error": f"Оплата получена, но выдать товар не удалось. Напишите {PAY_CONTACT}, заказ #{oid}.",
    }[r])


if __name__ == "__main__":
    app = ApplicationBuilder().token(os.environ["BOT_TOKEN"]).build()
    app.add_handler(CommandHandler(["start", "code"], start))
    app.add_handler(CommandHandler("newcode", newcode))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, paid))
    app.run_polling()
