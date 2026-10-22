"""API + WebSocket.  Запуск: uvicorn main:app --host 0.0.0.0 --port 8000
ENV: DB_URL, REDIS_URL, ADMIN_PASSWORD, MEDIA_DIR, BEHIND_PROXY"""
import asyncio
import json
import os
import secrets
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

import admin
from database import *  # noqa: F403

MEDIA = os.getenv("MEDIA_DIR", "media")
os.makedirs(MEDIA, exist_ok=True)
conns: dict[int, set[WebSocket]] = {}  # локальные соединения этого воркера


async def relay():
    """Слушает Redis и раздаёт события локальным WebSocket-ам."""
    ps = rds.pubsub()
    await ps.subscribe("ws")
    async for m in ps.listen():
        if m["type"] != "message":
            continue
        d = json.loads(m["data"])
        payload = json.dumps(d["data"])
        for uid in d["to"]:
            for ws in list(conns.get(uid, ())):
                try:
                    await ws.send_text(payload)
                except Exception:
                    conns[uid].discard(ws)


async def sampler():
    """Раз в минуту пишет число онлайн-пользователей для графика в админке."""
    while True:
        await rds.rpush("online_hist", await rds.scard("online"))
        await rds.ltrim("online_hist", -1440, -1)
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app):
    await init_db()
    async with SessionLocal() as s:
        ips = (await s.scalars(select(BannedIP.ip))).all()
        banned = (await s.scalars(select(User.id).where(User.is_banned))).all()
    if ips:
        await rds.sadd("banned_ips", *ips)
    if banned:
        await rds.sadd("banned_users", *banned)
    tasks = [asyncio.create_task(relay()), asyncio.create_task(sampler())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="DoraGram", lifespan=lifespan)
app.include_router(admin.router)
app.mount("/media", StaticFiles(directory=MEDIA), name="media")


@app.middleware("http")
async def ip_ban(request: Request, call_next):
    if await rds.sismember("banned_ips", ip_of(request)):
        return JSONResponse({"detail": "banned"}, status_code=403)
    return await call_next(request)


@app.exception_handler(Exception)
async def on_error(request: Request, exc: Exception):
    await log("error", f"{request.method} {request.url.path}: {exc!r}", ip_of(request))
    return JSONResponse({"detail": "server error"}, status_code=500)


async def auth(authorization: str = Header("")) -> User:
    async with SessionLocal() as s:
        sess = await s.get(AuthSession, authorization.removeprefix("Bearer ").strip())
        user = await s.get(User, sess.user_id) if sess else None
    if not user or user.is_banned:
        raise HTTPException(401, "unauthorized")
    return user


def udict(u: User) -> dict:
    return {"id": u.id, "name": u.name, "username": u.username, "bio": u.bio,
            "avatar": u.avatar, "phone": u.phone, "privilege": u.privilege, "badge": u.badge}


BADGES = {"verified": " [VERIFIED]", "scam": " [SCAM]", "fake": " [FAKE]"}


def label(u: User) -> str:
    return u.name + BADGES.get(u.badge, "")


class AuthIn(BaseModel):
    code: str = Field(max_length=16)


class ProfileIn(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=64)
    bio: str | None = Field(None, max_length=300)
    username: str | None = Field(None, pattern=r"^[A-Za-z0-9_]{5,32}$")  # 3–4 символа — только из магазина
    avatar: str | None = Field(None, pattern=r"^/media/[\w.-]+$")


class PrivateIn(BaseModel):
    user_id: int


class GroupIn(BaseModel):
    title: str = Field(min_length=1, max_length=64)
    members: list[int]


class GiftIn(BaseModel):
    gift_id: int
    to_user_id: int


# ---------- авторизация и профиль ----------

@app.post("/api/auth")
async def login(b: AuthIn, request: Request):
    ip = ip_of(request)
    n = await rds.incr(f"auth:{ip}")  # защита от перебора кода
    if n == 1:
        await rds.expire(f"auth:{ip}", 600)
    if n > 20:
        raise HTTPException(429, "too many attempts")
    async with SessionLocal() as s:
        u = await s.scalar(select(User).where(User.login_code == b.code.strip().upper()))
        if not u:
            await log("warn", "неверный код входа", ip)
            raise HTTPException(401, "bad code")
        if u.is_banned:
            raise HTTPException(403, "banned")
        token = secrets.token_urlsafe(32)
        s.add(AuthSession(token=token, user_id=u.id, ip=ip))
        await s.commit()
    return {"token": token, "user": udict(u)}


@app.get("/api/me")
async def me(u: User = Depends(auth)):
    return {**udict(u), "balance": u.balance}


@app.patch("/api/me")
async def edit_me(b: ProfileIn, u: User = Depends(auth)):
    async with SessionLocal() as s:
        row = await s.get(User, u.id)
        for k, v in b.model_dump(exclude_none=True).items():
            setattr(row, k, v)
        try:
            await s.commit()
        except IntegrityError:
            raise HTTPException(409, "username taken")
    return udict(row)


@app.get("/api/users/search")
async def search(q: str = "", u: User = Depends(auth)):
    like = f"%{q.strip().lstrip('@')}%"
    async with SessionLocal() as s:
        rows = (await s.scalars(select(User).where(
            User.id != u.id, ~User.is_banned, User.username != "doragram",
            (User.username.ilike(like)) | (User.name.ilike(like))).limit(20))).all()
    return [udict(x) for x in rows]


@app.get("/api/users/{uid}")
async def profile(uid: int, _: User = Depends(auth)):
    async with SessionLocal() as s:
        u = await s.get(User, uid)
        if not u:
            raise HTTPException(404)
        rows = (await s.execute(
            select(Gift, UserGift).join(UserGift, UserGift.gift_id == Gift.id)
            .where(UserGift.to_id == uid).order_by(UserGift.id.desc()).limit(100))).all()
    return {**udict(u), "online": bool(await rds.sismember("online", uid)),
            "last_seen": u.last_seen.isoformat(),
            "gifts": [{"emoji": g.emoji, "name": g.name, "from": ug.from_id} for g, ug in rows]}


# ---------- чаты ----------

@app.post("/api/chats/private")
async def private_chat(b: PrivateIn, u: User = Depends(auth)):
    async with SessionLocal() as s:
        if not await s.get(User, b.user_id):
            raise HTTPException(404)
        chat = await get_private(s, u.id, b.user_id)
        await s.commit()
    return {"chat_id": chat.id}


@app.post("/api/chats/group")
async def group_chat(b: GroupIn, u: User = Depends(auth)):
    ids = list(set(b.members) | {u.id})
    async with SessionLocal() as s:
        found = (await s.scalars(select(User.id).where(User.id.in_(ids)))).all()
        chat = Chat(type="group", title=b.title, created_by=u.id)
        s.add(chat)
        await s.flush()
        s.add_all([ChatMember(chat_id=chat.id, user_id=i) for i in found])
        await s.commit()
    return {"chat_id": chat.id}


@app.get("/api/chats")
async def chats(u: User = Depends(auth)):
    out = []
    async with SessionLocal() as s:
        for c in (await s.scalars(select(Chat).join(ChatMember, ChatMember.chat_id == Chat.id)
                                  .where(ChatMember.user_id == u.id))).all():
            last = await s.scalar(select(Message).where(Message.chat_id == c.id).order_by(Message.id.desc()).limit(1))
            unread = await s.scalar(select(func.count()).select_from(Message).where(
                Message.chat_id == c.id, Message.sender_id != u.id, Message.status == "sent"))
            title, peer = c.title, None
            if c.type == "private":
                peer = await s.scalar(select(ChatMember.user_id).where(
                    ChatMember.chat_id == c.id, ChatMember.user_id != u.id)) or u.id
                title = label(await s.get(User, peer))
            out.append({"id": c.id, "type": c.type, "title": title, "peer": peer, "unread": unread,
                        "last": msg_dict(last) if last else None})
    return sorted(out, key=lambda c: c["last"]["id"] if c["last"] else 0, reverse=True)


@app.get("/api/chats/{cid}/messages")
async def history(cid: int, before: int = 0, limit: int = 50, u: User = Depends(auth)):
    async with SessionLocal() as s:
        if u.id not in await members(s, cid):
            raise HTTPException(403)
        st = select(Message).where(Message.chat_id == cid).order_by(Message.id.desc()).limit(min(limit, 100))
        if before:
            st = st.where(Message.id < before)
        rows = (await s.scalars(st)).all()
    return [msg_dict(m) for m in reversed(rows)]


KINDS = {".jpg": "photo", ".jpeg": "photo", ".png": "photo", ".webp": "photo",
         ".mp4": "video", ".mov": "video", ".ogg": "voice", ".m4a": "voice", ".mp3": "voice", ".wav": "voice"}
MAX_UPLOAD = 50 * 2**20


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), _: User = Depends(auth)):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in KINDS:
        raise HTTPException(415, "unsupported type")
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "file too large")
    name = f"{uuid.uuid4().hex}{ext}"
    with open(os.path.join(MEDIA, name), "wb") as f:
        f.write(data)
    return {"url": f"/media/{name}", "type": KINDS[ext]}


# ---------- подарки ----------

@app.get("/api/gifts")
async def gifts(_: User = Depends(auth)):
    async with SessionLocal() as s:
        return [{"id": g.id, "emoji": g.emoji, "name": g.name, "price": g.price}
                for g in (await s.scalars(select(Gift).order_by(Gift.price))).all()]


@app.post("/api/gifts/send")
async def send_gift(b: GiftIn, u: User = Depends(auth)):
    async with SessionLocal() as s:
        g, to = await s.get(Gift, b.gift_id), await s.get(User, b.to_user_id)
        if not g or not to:
            raise HTTPException(404)
        r = await s.execute(update(User).where(User.id == u.id, User.balance >= g.price)
                            .values(balance=User.balance - g.price))
        if r.rowcount != 1:
            raise HTTPException(402, "Недостаточно средств")
        s.add(UserGift(gift_id=g.id, from_id=u.id, to_id=to.id))
        await s.commit()
    await publish([to.id], {"type": "gift", "from": u.id, "from_name": u.name, "gift": g.name, "emoji": g.emoji})
    return {"ok": True}


# ---------- магазин ----------

class FreeIn(BaseModel):
    prefix: str


class OrderIn(BaseModel):
    product_id: int
    number: str | None = Field(None, pattern=r"^\d{1,12}$")
    username: str | None = Field(None, pattern=r"^[A-Za-z0-9_]{3,4}$")
    gift_id: int | None = None


_bu = ""


async def bot_username() -> str:
    global _bu
    if not _bu:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"https://api.telegram.org/bot{os.environ['BOT_TOKEN']}/getMe")
        _bu = r.json()["result"]["username"]
    return _bu


@app.get("/api/shop")
async def shop(u: User = Depends(auth)):
    async with SessionLocal() as s:
        prods = (await s.scalars(select(Product).where(Product.active).order_by(Product.sort, Product.id))).all()
        used = await s.scalar(select(User.free_number_used).where(User.id == u.id))
    return {"free_prefixes": FREE_PREFIXES, "free_used": used,
            "products": [{"id": p.id, "kind": p.kind, "title": p.title, "price_rub": p.price_rub,
                          "price_xtr": p.price_xtr,
                          "digits": nat_len(p.data["prefix"]) if p.kind == "number_custom" else None}
                         for p in prods]}


@app.post("/api/numbers/free")
async def free_number(b: FreeIn, u: User = Depends(auth)):
    if b.prefix not in FREE_PREFIXES:
        raise HTTPException(400, "Недоступный префикс")
    async with SessionLocal() as s:
        r = await s.execute(update(User).where(User.id == u.id, ~User.free_number_used)
                            .values(free_number_used=True))
        if r.rowcount != 1:
            raise HTTPException(409, "Бесплатный номер уже получен")
        phone = await unique_number(s, b.prefix)
        await s.execute(update(User).where(User.id == u.id).values(phone=phone))
        await s.commit()
    return {"phone": phone}


@app.post("/api/shop/orders")
async def new_order(b: OrderIn, u: User = Depends(auth)):
    async with SessionLocal() as s:
        p = await s.get(Product, b.product_id)
        if not p or not p.active:
            raise HTTPException(404, "Товар не найден")
        params = {}
        if p.kind == "number_custom":
            n = nat_len(p.data["prefix"])
            if not b.number or len(b.number) != n:
                raise HTTPException(400, f"Нужно ровно {n} цифр")
            if await s.scalar(select(User.id).where(User.phone == "+" + p.data["prefix"] + b.number)):
                raise HTTPException(409, "Этот номер занят")
            params = {"number": b.number}
        elif p.kind == "username":
            if not b.username:
                raise HTTPException(400, "Нужен юзернейм из 3–4 символов")
            if await s.scalar(select(User.id).where(func.lower(User.username) == b.username.lower())):
                raise HTTPException(409, "Этот юзернейм занят")
            params = {"username": b.username}
        elif p.kind == "gift":
            if not b.gift_id or not await s.get(Gift, b.gift_id):
                raise HTTPException(400, "Выберите подарок")
            params = {"gift_id": b.gift_id}
        o = Order(user_id=u.id, product_id=p.id, params=params)
        s.add(o)
        await s.commit()
    return {"order_id": o.id, "link": f"https://t.me/{await bot_username()}?start=pay_{o.id}"}


# ---------- WebSocket ----------

async def mark_read(uid: int, cid: int):
    async with SessionLocal() as s:
        ids = await members(s, cid)
        if uid not in ids:
            return
        r = await s.execute(update(Message).where(
            Message.chat_id == cid, Message.sender_id != uid, Message.status == "sent").values(status="read"))
        await s.commit()
    if r.rowcount:
        await publish(ids, {"type": "read", "chat_id": cid, "by": uid})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, token: str = Query("")):
    async with SessionLocal() as s:
        sess = await s.get(AuthSession, token)
        user = await s.get(User, sess.user_id) if sess else None
    if not user or user.is_banned or await rds.sismember("banned_ips", ip_of(ws)):
        await ws.close(code=4403)
        return
    await ws.accept()
    uid = user.id
    conns.setdefault(uid, set()).add(ws)
    await rds.hincrby("conns", uid, 1)
    await rds.sadd("online", uid)
    try:
        while True:
            d = json.loads(await ws.receive_text())
            if await rds.sismember("banned_users", uid):
                break
            t = d.get("type")
            if t == "message":
                url = d.get("media_url")
                if url and not str(url).startswith("/media/"):
                    url = None
                await post_message(uid, int(d["chat_id"]), str(d.get("text") or "")[:4000],
                                   url, d.get("media_type") if url else None)
            elif t == "read":
                await mark_read(uid, int(d["chat_id"]))
            elif t == "ping":
                await ws.send_text('{"type":"pong"}')
    except (WebSocketDisconnect, ValueError, KeyError, TypeError):
        pass
    finally:
        conns[uid].discard(ws)
        if await rds.hincrby("conns", uid, -1) <= 0:
            await rds.srem("online", uid)
            await rds.hdel("conns", uid)
            async with SessionLocal() as s:
                await s.execute(update(User).where(User.id == uid).values(last_seen=now()))
                await s.commit()
        try:
            await ws.close()
        except Exception:
            pass
