"""Админ-панель: /admin  (пароль в ENV ADMIN_PASSWORD)"""
import os
import secrets

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from database import *  # noqa: F403

router = APIRouter(prefix="/admin")
PASSWORD = os.getenv("ADMIN_PASSWORD", "")


async def need_admin(admin_token: str | None = Cookie(None)):
    async with SessionLocal() as s:
        if not admin_token or not await s.get(AdminToken, admin_token):
            raise HTTPException(401)

A = [Depends(need_admin)]


class LoginIn(BaseModel):
    password: str


class EditIn(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=64)
    username: str | None = Field(None, pattern=r"^([A-Za-z0-9_]{3,32})?$")
    bio: str | None = Field(None, max_length=300)
    privilege: str | None = Field(None, pattern=r"^(user|vip|sponsor|moderator)$")
    badge: str | None = Field(None, pattern=r"^(verified|scam|fake)?$")
    balance: int | None = None


class BanIn(BaseModel):
    ban: bool
    ip: bool = False


class GrantIn(BaseModel):
    gift_id: int


class GiftIn(BaseModel):
    emoji: str = Field(max_length=8)
    name: str = Field(min_length=1, max_length=64)
    price: int = Field(ge=0)


class TextIn(BaseModel):
    text: str


@router.post("/api/login")
async def login(b: LoginIn, resp: Response, request: Request):
    ip = ip_of(request)
    if not PASSWORD or not secrets.compare_digest(b.password.encode(), PASSWORD.encode()):
        await log("warn", "неудачный вход в админку", ip)
        raise HTTPException(401)
    token = secrets.token_urlsafe(32)
    async with SessionLocal() as s:
        s.add(AdminToken(token=token))
        await s.commit()
    resp.set_cookie("admin_token", token, httponly=True, samesite="strict", max_age=7 * 86400)
    await log("info", "вход в админку", ip)
    return {"ok": True}


@router.get("/api/stats", dependencies=A)
async def stats():
    async with SessionLocal() as s:
        users = await s.scalar(text("select count(*) from users"))
        new24 = await s.scalar(text("select count(*) from users where created_at > now() - interval '24 hours'"))
        msgs = await s.scalar(text("select count(*) from messages"))
        hours = (await s.execute(text(
            "select count(m.id) from generate_series(date_trunc('hour', now()) - interval '23 hours', "
            "date_trunc('hour', now()), interval '1 hour') g "
            "left join messages m on date_trunc('hour', m.created_at) = g group by g order by g"))).scalars().all()
    hist = [int(x) for x in await rds.lrange("online_hist", -1440, -1)][::10] or [0]
    return {"users": users, "new24": new24, "messages": msgs, "online": await rds.scard("online"),
            "online_hist": hist, "msg_hours": list(hours)}


@router.get("/api/users", dependencies=A)
async def users(q: str = ""):
    st = select(User).order_by(User.id.desc()).limit(100)
    q = q.strip().lstrip("@")
    if q:
        like = f"%{q}%"
        st = st.where(User.name.ilike(like) | User.username.ilike(like) | User.phone.ilike(like)
                      | (User.id == (int(q) if q.isdigit() else -1)))
    async with SessionLocal() as s:
        rows = (await s.scalars(st)).all()
    online = await rds.smembers("online")
    return [{"id": u.id, "name": u.name, "username": u.username, "phone": u.phone, "bio": u.bio,
             "privilege": u.privilege, "badge": u.badge, "balance": u.balance, "banned": u.is_banned,
             "online": str(u.id) in online} for u in rows]


@router.patch("/api/users/{uid}", dependencies=A)
async def edit_user(uid: int, b: EditIn):
    data = b.model_dump(exclude_none=True)
    if "username" in data:
        data["username"] = data["username"] or None
    if data:
        async with SessionLocal() as s:
            await s.execute(update(User).where(User.id == uid).values(**data))
            try:
                await s.commit()
            except IntegrityError:
                raise HTTPException(409, "username taken")
        await log("info", f"admin: изменён пользователь {uid}: {', '.join(data)}")
    return {"ok": True}


@router.post("/api/users/{uid}/ban", dependencies=A)
async def ban_user(uid: int, b: BanIn):
    async with SessionLocal() as s:
        await s.execute(update(User).where(User.id == uid).values(is_banned=b.ban))
        ips = list((await s.scalars(select(AuthSession.ip).where(AuthSession.user_id == uid).distinct())).all())
        if b.ban and b.ip:
            for ip in ips:
                if not await s.get(BannedIP, ip):
                    s.add(BannedIP(ip=ip))
        await s.commit()
    if b.ban:
        await rds.sadd("banned_users", uid)
        if b.ip and ips:
            await rds.sadd("banned_ips", *ips)
    else:
        await rds.srem("banned_users", uid)
    await log("warn", f"admin: пользователь {uid} ban={b.ban} ip={b.ip}")
    return {"ok": True}


@router.post("/api/users/{uid}/gift", dependencies=A)
async def grant_gift(uid: int, b: GrantIn):
    async with SessionLocal() as s:
        s.add(UserGift(gift_id=b.gift_id, from_id=await system_user_id(), to_id=uid))
        await s.commit()
    return {"ok": True}


@router.get("/api/gifts", dependencies=A)
async def gifts():
    async with SessionLocal() as s:
        return [{"id": g.id, "emoji": g.emoji, "name": g.name, "price": g.price}
                for g in (await s.scalars(select(Gift).order_by(Gift.id))).all()]


@router.post("/api/gifts", dependencies=A)
async def add_gift(b: GiftIn):
    async with SessionLocal() as s:
        s.add(Gift(**b.model_dump()))
        await s.commit()
    return {"ok": True}


@router.post("/api/broadcast", dependencies=A)
async def broadcast(b: TextIn):
    msg = b.text.strip()[:4000]
    if not msg:
        raise HTTPException(400)
    sys_id = await system_user_id()
    async with SessionLocal() as s:
        ids = (await s.scalars(select(User.id).where(~User.is_banned, User.id != sys_id))).all()
    for uid in ids:
        async with SessionLocal() as s:
            chat = await get_private(s, sys_id, uid)
            await s.commit()
        await post_message(sys_id, chat.id, msg, system=True)
    await log("info", f"admin: рассылка {len(ids)} пользователям")
    return {"sent": len(ids)}


@router.get("/api/security", dependencies=A)
async def security():
    online = await rds.smembers("online")
    async with SessionLocal() as s:
        sess = (await s.execute(select(AuthSession, User.name).join(User, User.id == AuthSession.user_id)
                                .order_by(AuthSession.created_at.desc()).limit(100))).all()
        logs = (await s.scalars(select(LogEntry).order_by(LogEntry.id.desc()).limit(100))).all()
        ips = (await s.scalars(select(BannedIP.ip))).all()
    return {"sessions": [{"name": n, "ip": a.ip, "created": a.created_at.isoformat(),
                          "online": str(a.user_id) in online} for a, n in sess],
            "logs": [{"level": l.level, "text": l.text, "ip": l.ip, "created": l.created_at.isoformat()} for l in logs],
            "ips": list(ips)}


@router.post("/api/ips/unban", dependencies=A)
async def unban_ip(b: TextIn):
    async with SessionLocal() as s:
        row = await s.get(BannedIP, b.text)
        if row:
            await s.delete(row)
            await s.commit()
    await rds.srem("banned_ips", b.text)
    return {"ok": True}


class ProdIn(BaseModel):
    price_rub: int | None = Field(None, ge=0)
    price_xtr: int | None = Field(None, ge=1)
    active: bool | None = None


@router.get("/api/products", dependencies=A)
async def products():
    async with SessionLocal() as s:
        return [{"id": p.id, "title": p.title, "price_rub": p.price_rub, "price_xtr": p.price_xtr, "active": p.active}
                for p in (await s.scalars(select(Product).order_by(Product.sort, Product.id))).all()]


@router.patch("/api/products/{pid}", dependencies=A)
async def edit_product(pid: int, b: ProdIn):
    data = b.model_dump(exclude_none=True)
    if data:
        async with SessionLocal() as s:
            await s.execute(update(Product).where(Product.id == pid).values(**data))
            await s.commit()
        await log("info", f"admin: товар {pid} изменён: {data}")
    return {"ok": True}


@router.get("/api/orders", dependencies=A)
async def orders():
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(Order, Product.title, User.name).join(Product, Product.id == Order.product_id)
            .join(User, User.id == Order.user_id).order_by(Order.id.desc()).limit(100))).all()
    return [{"id": o.id, "user": n, "title": t, "params": o.params, "status": o.status, "method": o.method,
             "amount": o.amount, "created": o.created_at.isoformat()} for o, t, n in rows]


@router.post("/api/orders/{oid}/fulfill", dependencies=A)
async def manual_fulfill(oid: int):
    """Ручная выдача: оплата рублями подтверждена вне бота, либо повтор после ошибки."""
    r = await fulfill(oid, "manual", statuses=("pending", "error"))
    await log("info", f"admin: заказ #{oid} выдан вручную, результат: {r}")
    return {"result": r}


@router.get("", response_class=HTMLResponse)
async def page():
    return PAGE


PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DoraGram — админка</title>
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;600;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#ECEFF4;--card:#fff;--ink:#14213D;--mut:#5F6F86;--acc:#F2A541;--line:#D9DFE8;--bad:#C8453B}
*{box-sizing:border-box}
body{margin:0;font:15px/1.5 Manrope,system-ui,sans-serif;background:var(--bg);color:var(--ink)}
h1{font-size:26px;font-weight:800;margin:0 0 16px}h2{font-size:16px;margin:0 0 10px}
#app{display:grid;grid-template-columns:210px 1fr;min-height:100vh}
nav{background:var(--ink);padding:20px 10px;display:flex;flex-direction:column;gap:4px}
nav b{color:#fff;font-size:21px;font-weight:800;padding:0 12px 18px}
nav a{padding:9px 12px;border-radius:8px;color:#C9D2E3;text-decoration:none}
nav a.on{background:var(--acc);color:var(--ink);font-weight:600}
main{padding:24px;min-width:0;max-width:1100px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:14px}
.stats{display:flex;gap:16px;flex-wrap:wrap;color:var(--mut)}
.stats div{flex:1;min-width:130px}.stats b{display:block;font-size:30px;font-weight:800;color:var(--ink)}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.scroll{overflow-x:auto}table{width:100%;border-collapse:collapse}
td{padding:8px;border-bottom:1px solid var(--line);vertical-align:top}small{color:var(--mut)}
button{font:inherit;background:var(--ink);color:#fff;border:0;border-radius:8px;padding:7px 12px;cursor:pointer}
button.ghost{background:none;color:var(--ink);border:1px solid var(--line)}
button.bad{background:var(--bad)}b.bad{color:var(--bad)}
input,textarea,select{font:inherit;border:1px solid var(--line);border-radius:8px;padding:8px;width:100%}
label{display:block;margin:8px 0;color:var(--mut)}
dialog{border:1px solid var(--line);border-radius:12px;width:min(420px,92vw)}
tr.error td{color:var(--bad)}tr.warn td:nth-child(2){color:#B7791F;font-weight:600}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
#login{display:grid;place-items:center;min-height:100vh}#login form{width:min(340px,92vw)}
@media(max-width:760px){#app{grid-template-columns:1fr}nav{flex-direction:row;overflow-x:auto;padding:8px}nav b{display:none}main{padding:14px}}
</style></head><body>
<div id="login" hidden><form class="card" onsubmit="login();return false">
<h1>Вход в админку</h1><input id="pw" type="password" placeholder="Пароль" autocomplete="current-password">
<p><button>Войти</button> <span id="lerr" class="bad"></span></p></form></div>
<div id="app" hidden>
<nav><b>DoraGram</b><a href="#" data-v="stats">Обзор</a><a href="#" data-v="users">Пользователи</a>
<a href="#" data-v="gifts">Подарки</a><a href="#" data-v="shop">Магазин</a><a href="#" data-v="broadcast">Рассылка</a><a href="#" data-v="security">Безопасность</a></nav>
<main>
<section id="s_stats"></section>
<section id="s_users" hidden><h1>Пользователи</h1>
<div class="card"><input id="uq" placeholder="Имя, @username, телефон или ID" oninput="V.users()"></div>
<div class="card scroll"><table><tbody id="utable"></tbody></table></div></section>
<section id="s_gifts" hidden><h1>Подарки</h1>
<div class="card row"><input id="ge" placeholder="Эмодзи" style="max-width:90px"><input id="gn" placeholder="Название" style="flex:1"><input id="gp" type="number" placeholder="Цена" style="max-width:110px"><button onclick="addGift()">Добавить подарок</button></div>
<div class="card scroll"><table><tbody id="glist"></tbody></table></div></section>
<section id="s_shop" hidden></section>
<section id="s_broadcast" hidden><h1>Системная рассылка</h1>
<div class="card"><textarea id="bt" rows="5" placeholder="Текст придёт всем пользователям от имени DoraGram"></textarea>
<p class="row"><button onclick="sendAll()">Отправить всем</button><span id="bres"></span></p></div></section>
<section id="s_security" hidden></section>
</main></div>
<dialog id="dlg"><h2 id="dh"></h2>
<label>Имя<input id="en"></label><label>Юзернейм<input id="eu"></label><label>О себе<input id="eb"></label>
<label>Привилегия<select id="ep"><option>user</option><option>vip</option><option>sponsor</option><option>moderator</option></select></label><label>Метка<select id="eba"><option value="">нет</option><option value="verified">verified</option><option value="scam">scam</option><option value="fake">fake</option></select></label>
<label>Баланс<input id="el" type="number"></label>
<p class="row"><button onclick="saveU()">Сохранить</button><button class="ghost" onclick="$('#dlg').close()">Закрыть</button></p>
<label>Подарок<select id="eg"></select></label><button class="ghost" onclick="giveG()">Выдать подарок</button></dialog>
<script>
const $=s=>document.querySelector(s),
esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
dt=s=>esc(s.slice(0,19).replace('T',' '));
let cur=0,G=null;const U={};
async function api(p,body,method){
 const r=await fetch('/admin/api/'+p,{method:method||(body?'POST':'GET'),headers:{'Content-Type':'application/json'},body:body&&JSON.stringify(body)});
 if(r.status==401){show(0);throw 0}
 if(!r.ok){alert('Ошибка '+r.status);throw 0}
 return r.json()}
function line(v){const w=600,h=140,m=Math.max(1,...v);
 const p=v.map((y,i)=>`${i*w/Math.max(1,v.length-1)},${h-5-y/m*(h-12)}`).join(' ');
 return `<svg viewBox="0 0 ${w} ${h}" width="100%" role="img" aria-label="График онлайна"><polyline fill="none" stroke="#14213D" stroke-width="2.5" points="${p}"/></svg>`}
function bars(v){const w=600,h=140,m=Math.max(1,...v),bw=w/v.length;
 return `<svg viewBox="0 0 ${w} ${h}" width="100%" role="img" aria-label="Сообщения по часам">${v.map((y,i)=>`<rect x="${i*bw+2}" y="${h-y/m*(h-6)}" width="${bw-4}" height="${y/m*(h-6)}" fill="#F2A541"/>`).join('')}</svg>`}
const V={
 async stats(){const d=await api('stats');
  $('#s_stats').innerHTML=`<h1>Обзор</h1><div class="card stats"><div><b>${d.online}</b>сейчас онлайн</div><div><b>${d.users}</b>пользователей</div><div><b>${d.new24}</b>новых за сутки</div><div><b>${d.messages}</b>сообщений всего</div></div><div class="card"><h2>Онлайн за последние сутки</h2>${line(d.online_hist)}</div><div class="card"><h2>Сообщения по часам</h2>${bars(d.msg_hours)}</div>`},
 async users(){const d=await api('users?q='+encodeURIComponent($('#uq').value));
  $('#utable').innerHTML=d.map(u=>{U[u.id]=u;return `<tr><td>${u.id}</td><td>${esc(u.name)}<br><small>@${esc(u.username||'—')} ${esc(u.phone)}</small></td><td>${esc(u.privilege)}</td><td>${u.balance}</td><td>${u.banned?'<b class="bad">заблокирован</b>':u.online?'онлайн':'офлайн'}</td><td class="row"><button class="ghost" onclick="edit(${u.id})">Изменить</button><button class="${u.banned?'ghost':'bad'}" onclick="ban(${u.id},${!u.banned},false)">${u.banned?'Разбанить':'Бан'}</button>${u.banned?'':`<button class="bad" onclick="ban(${u.id},true,true)">Бан и IP</button>`}</td></tr>`}).join('')||'<tr><td>Никого не нашли</td></tr>'},
 async gifts(){G=await api('gifts');$('#glist').innerHTML=G.map(g=>`<tr><td>${esc(g.emoji)}</td><td>${esc(g.name)}</td><td>${g.price}</td></tr>`).join('')},
 async shop(){const [p,o]=await Promise.all([api('products'),api('orders')]);
  $('#s_shop').innerHTML=`<h1>Магазин</h1><div class="card scroll"><h2>Товары и цены</h2><table>${p.map(x=>`<tr><td>${esc(x.title)}</td><td><input id="pr${x.id}" type="number" value="${x.price_rub}" style="width:90px"> ₽</td><td><input id="px${x.id}" type="number" value="${x.price_xtr}" style="width:90px"> звёзд</td><td><label><input id="pa${x.id}" type="checkbox" ${x.active?'checked':''} style="width:auto"> в продаже</label></td><td><button onclick="saveP(${x.id})">Сохранить</button></td></tr>`).join('')}</table></div><div class="card scroll"><h2>Заказы</h2><table>${o.map(x=>`<tr><td>#${x.id}</td><td>${esc(x.user)}</td><td>${esc(x.title)} ${esc(JSON.stringify(x.params||{}))}</td><td>${esc(x.status)} ${esc(x.method||'')}</td><td>${dt(x.created)}</td><td>${x.status=='paid'?'':`<button class="ghost" onclick="giveO(${x.id})">Выдать вручную</button>`}</td></tr>`).join('')||'<tr><td>Заказов пока нет</td></tr>'}</table></div>`},
 broadcast(){},
 async security(){const d=await api('security');
  $('#s_security').innerHTML=`<h1>Безопасность</h1><div class="card scroll"><h2>Сессии</h2><table>${d.sessions.map(x=>`<tr><td>${esc(x.name)}</td><td>${esc(x.ip)}</td><td>${dt(x.created)}</td><td>${x.online?'онлайн':''}</td></tr>`).join('')}</table></div><div class="card scroll"><h2>Заблокированные IP</h2><table>${d.ips.map(i=>`<tr><td>${esc(i)}</td><td><button class="ghost" onclick="unIp('${esc(i)}')">Разблокировать</button></td></tr>`).join('')||'<tr><td>Заблокированных адресов нет</td></tr>'}</table></div><div class="card scroll"><h2>Журнал</h2><table>${d.logs.map(l=>`<tr class="${esc(l.level)}"><td>${dt(l.created)}</td><td>${esc(l.level)}</td><td>${esc(l.ip||'')}</td><td>${esc(l.text)}</td></tr>`).join('')}</table></div>`}
};
function go(n){document.querySelectorAll('main>section').forEach(s=>s.hidden=s.id!='s_'+n);
 document.querySelectorAll('nav a').forEach(a=>a.classList.toggle('on',a.dataset.v==n));V[n]()}
document.querySelectorAll('nav a').forEach(a=>a.onclick=e=>{e.preventDefault();go(a.dataset.v)});
function show(on){$('#login').hidden=!!on;$('#app').hidden=!on;if(on)go('stats')}
async function login(){const r=await fetch('/admin/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:$('#pw').value})});
 if(r.ok)show(1);else $('#lerr').textContent='Неверный пароль'}
async function ban(id,b,ip){if(!confirm(b?'Заблокировать пользователя'+(ip?' и его IP':'')+'?':'Снять блокировку?'))return;await api(`users/${id}/ban`,{ban:b,ip});V.users()}
async function edit(id){cur=id;const u=U[id];G=G||await api('gifts');
 $('#dh').textContent=u.name+' · #'+id;$('#en').value=u.name;$('#eu').value=u.username||'';$('#eb').value=u.bio||'';$('#ep').value=u.privilege;$('#eba').value=u.badge||'';$('#el').value=u.balance;
 $('#eg').innerHTML=G.map(g=>`<option value="${g.id}">${esc(g.emoji+' '+g.name)}</option>`).join('');$('#dlg').showModal()}
async function saveU(){await api('users/'+cur,{name:$('#en').value,username:$('#eu').value,bio:$('#eb').value,privilege:$('#ep').value,badge:$('#eba').value,balance:+$('#el').value},'PATCH');$('#dlg').close();V.users()}
async function giveG(){await api(`users/${cur}/gift`,{gift_id:+$('#eg').value});alert('Подарок выдан')}
async function addGift(){await api('gifts',{emoji:$('#ge').value,name:$('#gn').value,price:+$('#gp').value});V.gifts()}
async function saveP(id){await api('products/'+id,{price_rub:+$('#pr'+id).value,price_xtr:+$('#px'+id).value,active:$('#pa'+id).checked},'PATCH');V.shop()}
async function giveO(id){if(!confirm('Выдать товар по заказу #'+id+'? Деньги должны быть уже получены.'))return;const d=await api(`orders/${id}/fulfill`,{});alert('Результат: '+d.result);V.shop()}
async function unIp(ip){await api('ips/unban',{text:ip});V.security()}
async function sendAll(){const t=$('#bt').value.trim();if(!t||!confirm('Отправить сообщение всем пользователям?'))return;
 const d=await api('broadcast',{text:t});$('#bres').textContent='Отправлено: '+d.sent;$('#bt').value=''}
api('stats').then(()=>show(1)).catch(()=>{});
</script></body></html>"""
