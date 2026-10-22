"""Модели PostgreSQL, Redis и общие функции.
ENV: DB_URL, REDIS_URL, BEHIND_PROXY (=1, если API стоит за nginx)"""
import datetime as dt
import json
import os
import secrets

import redis.asyncio as aioredis
from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

engine = create_async_engine(
    os.getenv("DB_URL", "postgresql+asyncpg://dora:dora@localhost/doragram"),
    pool_size=20, max_overflow=30,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
rds = aioredis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)

now = lambda: dt.datetime.now(dt.timezone.utc)  # noqa: E731
TS = DateTime(timezone=True)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    phone: Mapped[str] = mapped_column(String(24), unique=True)  # виртуальный номер
    login_code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(64), default="User")
    bio: Mapped[str] = mapped_column(String(300), default="")
    avatar: Mapped[str | None] = mapped_column(String(255))
    privilege: Mapped[str] = mapped_column(String(16), default="user")  # user | vip | moderator
    balance: Mapped[int] = mapped_column(Integer, default=0)
    badge: Mapped[str] = mapped_column(String(12), default="", server_default="")  # "" | verified | scam | fake
    free_number_used: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen: Mapped[dt.datetime] = mapped_column(TS, default=now)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=now)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    ip: Mapped[str] = mapped_column(String(45))
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=now)


class Chat(Base):
    __tablename__ = "chats"
    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(8))  # private | group
    title: Mapped[str | None] = mapped_column(String(64))
    pair_key: Mapped[str | None] = mapped_column(String(32), unique=True)  # "minId:maxId"
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=now)


class ChatMember(Base):
    __tablename__ = "chat_members"
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), index=True)
    sender_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    text: Mapped[str] = mapped_column(Text, default="")
    media_url: Mapped[str | None] = mapped_column(String(255))
    media_type: Mapped[str | None] = mapped_column(String(10))  # photo | video | voice
    status: Mapped[str] = mapped_column(String(8), default="sent")  # sent | read
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=now, index=True)


class Gift(Base):
    __tablename__ = "gifts"
    id: Mapped[int] = mapped_column(primary_key=True)
    emoji: Mapped[str] = mapped_column(String(8))
    name: Mapped[str] = mapped_column(String(64))
    price: Mapped[int] = mapped_column(Integer, default=0)


class UserGift(Base):
    __tablename__ = "user_gifts"
    id: Mapped[int] = mapped_column(primary_key=True)
    gift_id: Mapped[int] = mapped_column(ForeignKey("gifts.id"))
    from_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    to_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=now)


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # number_random | number_custom | stars | sponsor | username | gift | badge_*
    title: Mapped[str] = mapped_column(String(64))
    price_rub: Mapped[int] = mapped_column(Integer, default=0)
    price_xtr: Mapped[int] = mapped_column(Integer, default=1)  # цена в Telegram Stars
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=100)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(8), default="pending")  # pending | paid | error
    method: Mapped[str | None] = mapped_column(String(8))  # xtr | rub | manual
    amount: Mapped[int | None] = mapped_column(Integer)
    charge_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=now)
    paid_at: Mapped[dt.datetime | None] = mapped_column(TS)


class AdminToken(Base):
    __tablename__ = "admin_tokens"
    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=now)


class BannedIP(Base):
    __tablename__ = "banned_ips"
    ip: Mapped[str] = mapped_column(String(45), primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=now)


class LogEntry(Base):
    __tablename__ = "logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    level: Mapped[str] = mapped_column(String(8))  # info | warn | error
    text: Mapped[str] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(String(45))
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=now, index=True)


# ---------- помощники ----------

def ip_of(conn) -> str:
    """IP клиента для Request/WebSocket. X-Forwarded-For доверяем только за своим nginx."""
    if os.getenv("BEHIND_PROXY") == "1":
        xff = conn.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return conn.client.host


def seed_products():
    P = [Product(kind="stars", title="1000 звёзд", price_rub=5, price_xtr=3, data={"amount": 1000}, sort=1),
         Product(kind="sponsor", title="Спонсорка", price_rub=150, price_xtr=85, sort=2),
         Product(kind="username", title="NFT-юзернейм (3–4 символа)", price_rub=40, price_xtr=20, sort=3),
         Product(kind="gift", title="Подарок из каталога", price_rub=100, price_xtr=50, sort=4),
         Product(kind="badge_verified", title="Верификация «мажор»", price_rub=100, price_xtr=50, data={"badge": "verified"}, sort=5),
         Product(kind="badge_scam", title="Метка «scam»", price_rub=50, price_xtr=25, data={"badge": "scam"}, sort=6),
         Product(kind="badge_fake", title="Метка «fake»", price_rub=50, price_xtr=25, data={"badge": "fake"}, sort=7)]
    for pre in ("888", "66", "880", "228"):  # цены на +66/+880/+228 — как у +888, правятся в админке
        P.append(Product(kind="number_random", title=f"Номер +{pre} (случайный)", price_rub=40, price_xtr=25, data={"prefix": pre}, sort=10))
        P.append(Product(kind="number_custom", title=f"Номер +{pre} (свой)", price_rub=60, price_xtr=35, data={"prefix": pre}, sort=11))
    return P


async def init_db():
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
        for col in ("badge VARCHAR(12) NOT NULL DEFAULT ''", "free_number_used BOOLEAN NOT NULL DEFAULT FALSE"):
            await c.execute(text(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col}"))
    async with SessionLocal() as s:
        if not await s.scalar(select(User).where(User.username == "doragram")):
            s.add(User(phone="+000000000", login_code=secrets.token_hex(8).upper(),
                       username="doragram", name="DoraGram"))
        if not await s.scalar(select(Gift.id).limit(1)):
            s.add_all([Gift(emoji=e, name=n, price=p) for e, n, p in
                       [("🧸", "Мишка", 50), ("🌹", "Роза", 30), ("🎂", "Торт", 100), ("🚀", "Ракета", 500)]])
        if not await s.scalar(select(Product.id).limit(1)):
            s.add_all(seed_products())
        await s.commit()


async def system_user_id() -> int:
    async with SessionLocal() as s:
        return await s.scalar(select(User.id).where(User.username == "doragram"))


async def log(level: str, text: str, ip: str | None = None):
    async with SessionLocal() as s:
        s.add(LogEntry(level=level, text=text[:2000], ip=ip))
        await s.commit()


async def publish(user_ids, payload: dict):
    """Доставка по WebSocket через Redis Pub/Sub — работает при нескольких воркерах."""
    await rds.publish("ws", json.dumps({"to": list(user_ids), "data": payload}))


async def members(s, chat_id: int) -> list[int]:
    return list((await s.scalars(select(ChatMember.user_id).where(ChatMember.chat_id == chat_id))).all())


async def get_private(s, a: int, b: int) -> Chat:
    """Находит или создаёт личный диалог. Вызывающий делает commit."""
    key = f"{min(a, b)}:{max(a, b)}"
    chat = await s.scalar(select(Chat).where(Chat.pair_key == key))
    if not chat:
        chat = Chat(type="private", pair_key=key, created_by=a)
        s.add(chat)
        await s.flush()
        s.add_all([ChatMember(chat_id=chat.id, user_id=u) for u in {a, b}])
    return chat


def msg_dict(m: Message) -> dict:
    return {"id": m.id, "chat_id": m.chat_id, "sender_id": m.sender_id, "text": m.text,
            "media_url": m.media_url, "media_type": m.media_type, "status": m.status,
            "system": m.is_system, "ts": m.created_at.isoformat()}


async def post_message(sender_id, chat_id, text="", media_url=None, media_type=None, system=False):
    async with SessionLocal() as s:
        ids = await members(s, chat_id)
        if sender_id not in ids:
            return None
        m = Message(chat_id=chat_id, sender_id=sender_id, text=text, media_url=media_url,
                    media_type=media_type, is_system=system)
        s.add(m)
        await s.commit()
    d = msg_dict(m)
    await publish(ids, {"type": "message", **d})
    return d


# ---------- магазин ----------

FREE_PREFIXES = ["52", "1", "7", "380", "3", "2"]  # бесплатно, один номер на пользователя


def nat_len(prefix: str) -> int:
    """Длина номера без кода страны."""
    return 8 if prefix == "888" else 9


def new_number(prefix: str) -> str:
    return "+" + prefix + "".join(secrets.choice("0123456789") for _ in range(nat_len(prefix)))


async def unique_number(s, prefix: str) -> str:
    for _ in range(100):
        phone = new_number(prefix)
        if not await s.scalar(select(User.id).where(User.phone == phone)):
            return phone
    raise RuntimeError("не удалось подобрать свободный номер")


async def apply_product(s, o: Order, p: Product):
    u = await s.get(User, o.user_id)
    d, prm = p.data or {}, o.params or {}
    if p.kind == "number_random":
        u.phone = await unique_number(s, d["prefix"])
    elif p.kind == "number_custom":
        u.phone = "+" + d["prefix"] + prm["number"]
    elif p.kind == "stars":
        u.balance += d["amount"]
    elif p.kind == "sponsor":
        u.privilege = "sponsor"
    elif p.kind == "username":
        u.username = prm["username"]
    elif p.kind == "gift":
        s.add(UserGift(gift_id=prm["gift_id"], from_id=await system_user_id(), to_id=u.id))
    elif p.kind.startswith("badge_"):
        u.badge = d["badge"]
    await s.flush()  # конфликт уникальности (номер/юзернейм) вылетит здесь


async def fulfill(order_id: int, method: str, amount: int | None = None, charge: str | None = None,
                  statuses=("pending",)) -> str:
    """Идемпотентная выдача товара. Возвращает ok | already | error."""
    async with SessionLocal() as s:
        r = await s.execute(update(Order).where(Order.id == order_id, Order.status.in_(statuses))
                            .values(status="paid", method=method, amount=amount, charge_id=charge, paid_at=now()))
        if r.rowcount != 1:
            return "already"
        o = await s.get(Order, order_id)
        p = await s.get(Product, o.product_id)
        uid, title = o.user_id, p.title
        try:
            await apply_product(s, o, p)
            await s.commit()
        except Exception as e:
            await s.rollback()
            async with SessionLocal() as s2:
                await s2.execute(update(Order).where(Order.id == order_id)
                                 .values(status="error", method=method, amount=amount, charge_id=charge))
                await s2.commit()
            await log("error", f"заказ #{order_id} оплачен, но не выдан: {e!r}")
            return "error"
    await publish([uid], {"type": "order", "order_id": order_id, "title": title})
    return "ok"
