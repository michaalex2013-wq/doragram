"""Клиент DoraGram на Kivy. Адрес сервера — в HOST."""
import json
import os
import threading
import time
import webbrowser

import requests
import websocket  # websocket-client
from kivy.app import App
from kivy.clock import Clock
from kivy.factory import Factory
from kivy.lang import Builder
from kivy.metrics import dp
from kivy.storage.jsonstore import JsonStore
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.screenmanager import Screen
from kivy.uix.textinput import TextInput
from kivy.utils import escape_markup

HOST = "177.3.221.40"
API = f"http://{HOST}"
WS = f"ws://{HOST}/ws"

KV = """
<Msg@Label>:
    size_hint_y: None
    markup: True
    text_size: self.width - dp(16), None
    height: self.texture_size[1] + dp(10)

ScreenManager:
    LoginScreen:
    ChatsScreen:
    ChatScreen:
    ShopScreen:

<LoginScreen>:
    name: 'login'
    BoxLayout:
        orientation: 'vertical'
        padding: dp(24)
        spacing: dp(12)
        Label:
            text: 'DoraGram'
            font_size: sp(34)
        Label:
            text: 'Получите код у Telegram-бота и введите его здесь'
            text_size: self.width, None
            halign: 'center'
            size_hint_y: None
            height: dp(48)
        TextInput:
            id: code
            multiline: False
            hint_text: 'Код доступа'
            size_hint_y: None
            height: dp(46)
            on_text_validate: app.login(self.text)
        Button:
            text: 'Войти'
            size_hint_y: None
            height: dp(48)
            on_release: app.login(code.text)
        Label:
            id: err
            color: 1, .4, .4, 1

<ChatsScreen>:
    name: 'chats'
    BoxLayout:
        orientation: 'vertical'
        BoxLayout:
            size_hint_y: None
            height: dp(48)
            TextInput:
                multiline: False
                hint_text: 'Найти по имени или @username'
                on_text_validate: app.search(self.text)
            Button:
                text: 'Чаты'
                size_hint_x: None
                width: dp(72)
                on_release: app.load_chats()
            Button:
                text: 'Магазин'
                size_hint_x: None
                width: dp(92)
                on_release: app.open_shop()
        ScrollView:
            GridLayout:
                id: rows
                cols: 1
                size_hint_y: None
                height: self.minimum_height
                spacing: dp(2)

<ChatScreen>:
    name: 'chat'
    BoxLayout:
        orientation: 'vertical'
        BoxLayout:
            size_hint_y: None
            height: dp(48)
            Button:
                text: '<'
                size_hint_x: None
                width: dp(48)
                on_release: app.back()
            Label:
                id: title
            Button:
                text: 'Подарок'
                size_hint_x: None
                width: dp(88)
                on_release: app.gifts()
        ScrollView:
            id: sv
            GridLayout:
                id: msgs
                cols: 1
                size_hint_y: None
                height: self.minimum_height
        BoxLayout:
            size_hint_y: None
            height: dp(48)
            TextInput:
                id: inp
                multiline: False
                hint_text: 'Сообщение'
                on_text_validate:
                    app.send(self.text)
                    self.text = ''
            Button:
                text: 'Отправить'
                size_hint_x: None
                width: dp(100)
                on_release:
                    app.send(inp.text)
                    inp.text = ''

<ShopScreen>:
    name: 'shop'
    BoxLayout:
        orientation: 'vertical'
        BoxLayout:
            size_hint_y: None
            height: dp(56)
            Button:
                text: '<'
                size_hint_x: None
                width: dp(48)
                on_release: app.root.current = 'chats'
            Label:
                id: info
        ScrollView:
            GridLayout:
                id: rows
                cols: 1
                size_hint_y: None
                height: self.minimum_height
                spacing: dp(2)
"""


class LoginScreen(Screen):
    pass


class ChatsScreen(Screen):
    pass


class ChatScreen(Screen):
    pass


class ShopScreen(Screen):
    pass


class DoraApp(App):
    token = ""
    me = None
    chat = None  # {"id", "title", "peer"}
    ws = None
    _ws_on = False

    # ---------- запуск ----------
    def build(self):
        self.title = "DoraGram"
        self.store = JsonStore(os.path.join(self.user_data_dir, "dora.json"))
        return Builder.load_string(KV)

    def on_start(self):
        if self.store.exists("auth"):
            self.token = self.store.get("auth")["token"]
            self.call("GET", "/api/me", self.on_me)

    def scr(self, name):
        return self.root.get_screen(name)

    # ---------- сеть ----------
    def call(self, method, path, cb, **kw):
        def run():
            try:
                r = requests.request(method, API + path, timeout=15,
                                     headers={"Authorization": "Bearer " + self.token}, **kw)
                ok, data = r.ok, (r.json() if r.content else {})
            except Exception as e:
                ok, data = False, {"detail": str(e)}
            Clock.schedule_once(lambda dt: cb(ok, data))
        threading.Thread(target=run, daemon=True).start()

    def start_ws(self):
        if self._ws_on:
            return
        self._ws_on = True

        def run():
            while self.token:
                self.ws = websocket.WebSocketApp(
                    f"{WS}?token={self.token}",
                    on_message=lambda w, m: Clock.schedule_once(lambda dt: self.on_ws(json.loads(m))),
                    on_error=lambda w, e: getattr(e, "status_code", 0) == 403
                    and Clock.schedule_once(lambda dt: self.logout()),
                )
                try:
                    self.ws.run_forever(ping_interval=20, ping_timeout=10)
                except Exception:
                    pass
                time.sleep(3)
            self._ws_on = False
        threading.Thread(target=run, daemon=True).start()

    def ws_send(self, d):
        try:
            self.ws.send(json.dumps(d))
        except Exception:
            pass

    # ---------- вход ----------
    def login(self, code):
        self.call("POST", "/api/auth", self.on_login, json={"code": code.strip()})

    def on_login(self, ok, d):
        if not ok:
            self.scr("login").ids.err.text = "Неверный код или доступ закрыт"
            return
        self.token, self.me = d["token"], d["user"]
        self.store.put("auth", token=self.token)
        self.enter()

    def on_me(self, ok, d):
        if ok:
            self.me = d
            self.enter()
        else:
            self.token = ""

    def enter(self):
        self.root.current = "chats"
        self.start_ws()
        self.load_chats()

    def logout(self):
        self.token = ""
        if self.ws:
            self.ws.close()
        if self.store.exists("auth"):
            self.store.delete("auth")
        self.root.current = "login"

    # ---------- список чатов ----------
    def row(self, grid, text, cb):
        b = Button(text=text, size_hint_y=None, height=dp(64), halign="left", valign="middle")
        b.bind(size=lambda w, s: setattr(w, "text_size", (s[0] - dp(20), None)),
               on_release=lambda *_: cb())
        grid.add_widget(b)

    def load_chats(self):
        self.call("GET", "/api/chats", self.show_chats)

    def show_chats(self, ok, d):
        g = self.scr("chats").ids.rows
        g.clear_widgets()
        if not ok:
            return
        for c in d:
            last = (c["last"] or {}).get("text") or "…"
            unread = f"  [{c['unread']}]" if c["unread"] else ""
            self.row(g, f"{c['title']}{unread}\n{last[:60]}",
                     lambda c=c: self.open_chat(c["id"], c["title"], c["peer"]))

    def search(self, q):
        self.call("GET", "/api/users/search", self.show_found, params={"q": q.strip()})

    def show_found(self, ok, d):
        g = self.scr("chats").ids.rows
        g.clear_widgets()
        for u in (d if ok else []):
            self.row(g, f"{u['name']}   @{u['username'] or '—'}", lambda u=u: self.start_private(u))

    def start_private(self, u):
        self.call("POST", "/api/chats/private",
                  lambda ok, d: ok and self.open_chat(d["chat_id"], u["name"], u["id"]),
                  json={"user_id": u["id"]})

    # ---------- чат ----------
    def open_chat(self, cid, title, peer):
        self.chat = {"id": cid, "title": title, "peer": peer}
        self.scr("chat").ids.title.text = title
        self.root.current = "chat"
        self.load_msgs()

    def back(self):
        self.chat = None
        self.root.current = "chats"
        self.load_chats()

    def load_msgs(self):
        self.call("GET", f"/api/chats/{self.chat['id']}/messages", self.show_msgs)

    def show_msgs(self, ok, d):
        if not ok or not self.chat:
            return
        self.scr("chat").ids.msgs.clear_widgets()
        for m in d:
            self.add_msg(m)
        self.mark_read()

    def add_msg(self, m):
        mine = m["sender_id"] == self.me["id"]
        who = "[b]Система[/b]  " if m["system"] else ""
        tail = "  • прочитано" if mine and m["status"] == "read" else ""
        body = (m["text"] or "") + ("  [медиа]" if m["media_url"] else "")
        ids = self.scr("chat").ids
        ids.msgs.add_widget(Factory.Msg(text=f"{who}{escape_markup(body)}{tail}",
                                        halign="right" if mine else "left"))
        Clock.schedule_once(lambda dt: setattr(ids.sv, "scroll_y", 0), .1)

    def send(self, text):
        if self.chat and text.strip():
            self.ws_send({"type": "message", "chat_id": self.chat["id"], "text": text.strip()})

    def mark_read(self):
        if self.chat:
            self.ws_send({"type": "read", "chat_id": self.chat["id"]})

    def on_ws(self, d):
        t = d.get("type")
        cur = bool(self.chat) and self.root.current == "chat" and d.get("chat_id") == self.chat["id"]
        if t == "message":
            if cur:
                self.add_msg(d)
                if d["sender_id"] != self.me["id"]:
                    self.mark_read()
            elif self.root.current == "chats":
                self.load_chats()
        elif t == "read" and cur and d.get("by") != self.me["id"]:
            self.load_msgs()
        elif t == "gift":
            self.toast(f"{d['from_name']} подарил(а) вам: {d['gift']}")
        elif t == "order":
            self.toast(f"Заказ #{d['order_id']} выдан: {d['title']}")
            if self.root.current == "shop":
                self.open_shop()

    # ---------- магазин ----------
    BADGES = {"verified": "VERIFIED", "scam": "SCAM", "fake": "FAKE"}

    def open_shop(self):
        self.root.current = "shop"
        self.call("GET", "/api/me", self.on_shop_me)
        self.call("GET", "/api/shop", self.show_shop)

    def on_shop_me(self, ok, d):
        if ok:
            self.me = d
            b = f"  [{self.BADGES[d['badge']]}]" if d.get("badge") else ""
            self.scr("shop").ids.info.text = f"Ваш номер: {d['phone']}{b}\nБаланс: {d['balance']} звёзд"

    def show_shop(self, ok, d):
        g = self.scr("shop").ids.rows
        g.clear_widgets()
        if not ok:
            return
        if not d["free_used"]:
            for p in d["free_prefixes"]:
                self.row(g, f"Бесплатный номер +{p}\nможно взять только один",
                         lambda p=p: self.call("POST", "/api/numbers/free", self.on_claimed, json={"prefix": p}))
        for p in d["products"]:
            self.row(g, f"{p['title']}\n{p['price_rub']} ₽  /  {p['price_xtr']} звёзд Telegram",
                     lambda p=p: self.buy(p))

    def on_claimed(self, ok, d):
        self.toast(f"Ваш номер: {d['phone']}" if ok else str(d.get("detail", "Ошибка")))
        self.open_shop()

    def buy(self, p):
        k = p["kind"]
        if k == "number_custom":
            self.ask(f"Введите {p['digits']} цифр номера", lambda t: self.order(p["id"], number=t.strip()))
        elif k == "username":
            self.ask("Юзернейм из 3–4 символов (a-z, 0-9, _)", lambda t: self.order(p["id"], username=t.strip()))
        elif k == "gift":
            self.pick_gift(lambda gid: self.order(p["id"], gift_id=gid))
        else:
            self.order(p["id"])

    def ask(self, title, cb):
        box = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(8))
        ti = TextInput(multiline=False, size_hint_y=None, height=dp(44))
        ok = Button(text="Продолжить", size_hint_y=None, height=dp(44))
        box.add_widget(ti)
        box.add_widget(ok)
        pop = Popup(title=title, content=box, size_hint=(.9, .35))
        ok.bind(on_release=lambda *_: (pop.dismiss(), cb(ti.text)))
        pop.open()

    def pick_gift(self, cb):
        def show(ok, d):
            if not ok:
                return
            box = BoxLayout(orientation="vertical", spacing=dp(4))
            pop = Popup(title="Выберите подарок", content=box, size_hint=(.9, .7))
            for g in d:
                b = Button(text=g["name"])
                b.bind(on_release=lambda w, g=g: (pop.dismiss(), cb(g["id"])))
                box.add_widget(b)
            pop.open()
        self.call("GET", "/api/gifts", show)

    def order(self, pid, **extra):
        self.call("POST", "/api/shop/orders", self.on_order, json={"product_id": pid, **extra})

    def on_order(self, ok, d):
        if not ok:
            self.toast(str(d.get("detail", "Ошибка")))
            return
        self.toast(f"Заказ #{d['order_id']} создан. Открываю бота для оплаты.")
        webbrowser.open(d["link"])

    # ---------- подарки ----------
    def toast(self, text):
        Popup(title="DoraGram", content=Label(text=text), size_hint=(.85, .25)).open()

    def gifts(self):
        if self.chat and self.chat["peer"]:
            self.call("GET", "/api/gifts", self.show_gifts)

    def show_gifts(self, ok, d):
        if not ok:
            return
        box = BoxLayout(orientation="vertical", spacing=dp(4))
        pop = Popup(title="Отправить подарок", content=box, size_hint=(.9, .7))
        for g in d:
            b = Button(text=f"{g['name']} — {g['price']}")
            b.bind(on_release=lambda w, g=g: (pop.dismiss(), self.send_gift(g["id"])))
            box.add_widget(b)
        pop.open()

    def send_gift(self, gid):
        self.call("POST", "/api/gifts/send",
                  lambda ok, d: self.toast("Подарок отправлен" if ok else d.get("detail", "Ошибка")),
                  json={"gift_id": gid, "to_user_id": self.chat["peer"]})


if __name__ == "__main__":
    DoraApp().run()
