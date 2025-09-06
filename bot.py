import os
import json
import requests
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from dotenv import load_dotenv


from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ParseMode, WebAppInfo, KeyboardButton, ReplyKeyboardMarkup
)
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Updater, CommandHandler, CallbackQueryHandler, MessageHandler,
    Filters, CallbackContext, ConversationHandler, MessageFilter
)

# =========================
# WebApp data filter (PTB v13)
# =========================
class HasWebAppData(MessageFilter):
    def filter(self, message):
        return bool(getattr(message, "web_app_data", None))

has_webapp_data = HasWebAppData()

# =========================
# Healthcheck HTTP (Fly.io)
# =========================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

def start_health_server():
    port = int(os.getenv("PORT", "8080"))
    httpd = HTTPServer(("0.0.0.0", port), HealthHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

# =========================
# Config / Chargement
# =========================
load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")  # optionnel
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()

# Produits : soit local products.json, soit URL (même que ta Mini-App)
PRODUCTS_PATH = Path(__file__).with_name("products.json")
PRODUCTS_URL = os.getenv("PRODUCTS_URL", "").strip()

ORDERS_PATH = Path(__file__).with_name("orders.jsonl")  # JSON lines robuste

# Etats conversation checkout
ASK_NAME, ASK_ADDRESS, ASK_PHONE, ASK_CONFIRM = range(4)

def load_products() -> List[Dict]:
    # Essaye l’URL d’abord si fournie
    if PRODUCTS_URL:
        try:
            r = requests.get(PRODUCTS_URL, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"[WARN] Echec PRODUCTS_URL ({PRODUCTS_URL}): {e}. On bascule sur le fichier local.")
    # Fallback : fichier local
    with open(PRODUCTS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


PRODUCTS: List[Dict] = load_products()
# ids en str pour uniformiser
for p in PRODUCTS:
    p["id"] = str(p["id"])
PRODUCT_INDEX: Dict[str, Dict] = {p["id"]: p for p in PRODUCTS}

def save_order(order: Dict):
    with open(ORDERS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(order, ensure_ascii=False) + "\n")

# =========================
# Utilitaires panier/format
# =========================
def format_price(v) -> str:
    return f"{float(v):.2f} €".replace(".", ",")

def ensure_cart(context: CallbackContext) -> List[Dict]:
    if "cart" not in context.user_data:
        context.user_data["cart"] = []
    return context.user_data["cart"]

def get_cart_total(cart: List[Dict]) -> float:
    return sum(float(i["price"]) * int(i["qty"]) for i in cart)

def item_key(pid: str, variant: Optional[str]) -> str:
    return f"{pid}::{variant or ''}"

def product_by_id(pid: str) -> Optional[Dict]:
    return PRODUCT_INDEX.get(str(pid))

def add_item_to_cart(context: CallbackContext, product: Dict, variant: Optional[str] = None, qty: int = 1):
    cart = ensure_cart(context)
    key = item_key(product["id"], variant)
    for it in cart:
        if it.get("key") == key:
            it["qty"] += qty
            break
    else:
        cart.append({
            "key": key,
            "id": product["id"],
            "name": product["name"],
            "price": float(product["price"]),
            "image": product.get("image"),
            "variant": variant,
            "qty": int(qty)
        })

def remove_item_from_cart(context: CallbackContext, key: str):
    cart = ensure_cart(context)
    context.user_data["cart"] = [it for it in cart if it.get("key") != key]

def display_label(i: Dict) -> str:
    v = i.get("variant")
    return f"{i['name']} ({v})" if v else i["name"]

# =========================
# Claviers & textes
# =========================
def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛍️ Voir les produits", callback_data="shop")],
        [InlineKeyboardButton("🧺 Voir le panier", callback_data="cart")]
    ])

def back_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Retour boutique", callback_data="shop"),
         InlineKeyboardButton("🧺 Panier", callback_data="cart")]
    ])

def product_detail_text(p: Dict) -> str:
    lines = [f"*{p['name']}*", f"{format_price(p['price'])}", ""]
    if p.get("description"):
        lines.append(p["description"])
    return "\n".join(lines)

def render_cart_text_and_kb(context: CallbackContext):
    cart = ensure_cart(context)
    if not cart:
        text = "🧺 *Votre panier est vide.*"
        kb = [[InlineKeyboardButton("🛍️ Retour boutique", callback_data="shop")]]
        return text, InlineKeyboardMarkup(kb)

    total = 0.0
    lines = []
    kb_rows = []
    for it in cart:
        label = it["name"] + (f" — Taille {it['variant']}" if it.get("variant") else "")
        subtotal = float(it["price"]) * int(it["qty"])
        total += subtotal
        lines.append(f"• {label} x{it['qty']} — {format_price(subtotal)}")
        kb_rows.append([InlineKeyboardButton(f"Retirer {label}", callback_data=f"rm_{it['key']}")])

    lines.append(f"\n*Total :* {format_price(total)}")
    actions = [
        InlineKeyboardButton("✅ Passer commande", callback_data="checkout"),
        InlineKeyboardButton("🧹 Vider", callback_data="clearcart"),
    ]
    kb_rows.append(actions)
    kb_rows.append([InlineKeyboardButton("🛍️ Retour boutique", callback_data="shop")])
    return "🧺 *Votre panier*\n\n" + "\n".join(lines), InlineKeyboardMarkup(kb_rows)

# =========================
# Affichages (liste / fiche / panier)
# =========================
def send_product_list(update: Update, context: CallbackContext, query_msg=None):
    text = "*Produits disponibles*\nSélectionnez un article pour voir les détails."
    kb = []
    for p in PRODUCTS:
        kb.append([InlineKeyboardButton(f"{p['name']} — {format_price(p['price'])}",
                                        callback_data=f"prod_{p['id']}")])
    kb.append([InlineKeyboardButton("🧺 Panier", callback_data="cart")])
    markup = InlineKeyboardMarkup(kb)

    if query_msg:
        try:
            if getattr(query_msg, "photo", None):
                context.bot.delete_message(chat_id=query_msg.chat_id, message_id=query_msg.message_id)
                context.bot.send_message(chat_id=query_msg.chat_id, text=text,
                                         parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
            else:
                query_msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
        except BadRequest:
            context.bot.send_message(chat_id=query_msg.chat_id, text=text,
                                     parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
    else:
        update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)

def send_product_detail(update: Update, context: CallbackContext, prod_id: str, edit_existing: bool = False):
    p = product_by_id(prod_id)
    if not p:
        msg = update.callback_query.message if update.callback_query else update.effective_message
        try:
            msg.edit_text("❌ Produit introuvable.", reply_markup=back_menu_kb())
        except Exception:
            update.effective_chat.send_message("❌ Produit introuvable.", reply_markup=back_menu_kb())
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Ajouter au panier", callback_data=f"add_{prod_id}")],
        [InlineKeyboardButton("⬅️ Retour boutique", callback_data="shop"),
         InlineKeyboardButton("🧺 Panier", callback_data="cart")]
    ])
    caption = product_detail_text(p)
    image_url = p.get("image")

    msg = update.callback_query.message if update.callback_query else None
    try:
        if edit_existing and msg:
            if getattr(msg, "photo", None) and image_url:
                msg.edit_caption(caption=caption, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
            elif image_url:
                msg.delete()
                context.bot.send_photo(chat_id=msg.chat_id, photo=image_url,
                                       caption=caption, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
            else:
                msg.edit_text(caption, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        else:
            if image_url:
                if msg:
                    msg.delete()
                    context.bot.send_photo(chat_id=msg.chat_id, photo=image_url,
                                           caption=caption, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
                else:
                    update.effective_chat.send_photo(photo=image_url,
                                                     caption=caption, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
            else:
                if msg:
                    msg.edit_text(caption, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
                else:
                    update.effective_message.reply_text(caption, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except BadRequest:
        chat_id = msg.chat_id if msg else update.effective_chat.id
        if image_url:
            context.bot.send_photo(chat_id=chat_id, photo=image_url,
                                   caption=caption, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        else:
            context.bot.send_message(chat_id=chat_id, text=caption,
                                     parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

def send_cart(update: Update, context: CallbackContext, query_msg=None):
    text, markup = render_cart_text_and_kb(context)
    if query_msg:
        try:
            if getattr(query_msg, "photo", None):
                context.bot.delete_message(chat_id=query_msg.chat_id, message_id=query_msg.message_id)
                context.bot.send_message(chat_id=query_msg.chat_id, text=text,
                                         parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
            else:
                query_msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
        except BadRequest:
            context.bot.send_message(chat_id=query_msg.chat_id, text=text,
                                     parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
    else:
        update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)

# =========================
# Commandes
# =========================
def start(update: Update, context: CallbackContext):
    buttons = []
    if WEBAPP_URL:
        buttons.append([KeyboardButton("🧾 Ouvrir la boutique", web_app=WebAppInfo(url=WEBAPP_URL))])
    reply_kb = ReplyKeyboardMarkup(buttons, resize_keyboard=True) if buttons else None

    update.message.reply_text(
        "👋 Bienvenue dans ma boutique Telegram !\n\n"
        "• 🛍️ Utilise les boutons ci-dessous, ou /shop pour la version bot",
        reply_markup=reply_kb or main_menu_kb()
    )

def help_cmd(update: Update, context: CallbackContext):
    update.message.reply_text(
        "Commandes utiles :\n"
        "/start — menu principal\n"
        "/shop — liste des produits\n"
        "/cart — voir le panier\n"
    )

def shop_cmd(update: Update, context: CallbackContext):
    send_product_list(update, context)

def cart_cmd(update: Update, context: CallbackContext):
    send_cart(update, context)

# =========================
# CallbackQuery handlers (boutique)
# =========================
def on_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    data = query.data
    query.answer()

    if data == "shop":
        send_product_list(update, context, query_msg=query.message)
        return

    if data == "cart":
        send_cart(update, context, query_msg=query.message)
        return

    if data == "clearcart":
        context.user_data["cart"] = []
        try:
            query.edit_message_text("🧺 Panier vidé.", reply_markup=back_menu_kb())
        except BadRequest:
            context.bot.send_message(chat_id=query.message.chat_id, text="🧺 Panier vidé.", reply_markup=back_menu_kb())
        return

    if data.startswith("prod_"):
        pid = data.split("_", 1)[1]
        send_product_detail(update, context, prod_id=pid, edit_existing=True)
        return

# Ajouter au panier (gère tailles)
def add_to_cart_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    pid = query.data.split("_", 1)[1]
    prod = product_by_id(pid)
    if not prod:
        query.answer("Produit introuvable", show_alert=True)
        return

    variants = prod.get("variants") or []
    if variants:
        kb = [[InlineKeyboardButton(v, callback_data=f"choose_{pid}_{v}")] for v in variants]
        kb.append([InlineKeyboardButton("⬅️ Retour", callback_data=f"prod_{pid}")])
        try:
            query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(kb))
        except BadRequest:
            context.bot.send_message(chat_id=query.message.chat_id, text="Choisis une taille :",
                                     reply_markup=InlineKeyboardMarkup(kb))
        query.answer("Choisis une taille")
        return

    add_item_to_cart(context, prod, variant=None, qty=1)
    query.answer("Ajouté au panier ✅", show_alert=False)

# Choix de la taille
def choose_variant_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    _, pid, variant = query.data.split("_", 2)
    prod = product_by_id(pid)
    if not prod:
        query.answer("Produit introuvable", show_alert=True)
        return
    if variant not in (prod.get("variants") or []):
        query.answer("Taille invalide", show_alert=True)
        return

    add_item_to_cart(context, prod, variant=variant, qty=1)
    # réafficher la fiche pour les actions
    send_product_detail(update, context, prod_id=pid, edit_existing=True)
    query.answer(f"Ajouté ({variant}) ✅")

# Retirer un item (clé = id::variant)
def remove_from_cart_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    key = query.data.split("_", 1)[1]
    remove_item_from_cart(context, key)
    send_cart(update, context, query_msg=query.message)
    query.answer("Retiré ✅")



# =========================
# Checkout (Conversation)
# =========================
def start_checkout_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()

    cart = ensure_cart(context)
    if not cart:
        query.answer("Votre panier est vide.", show_alert=True)
        return ConversationHandler.END

    context.user_data["order"] = {"cart": cart}
    query.edit_message_text(
        "📝 *Commande — Étape 1/3*\n\nQuel est votre *nom complet* ?",
        parse_mode=ParseMode.MARKDOWN
    )
    return ASK_NAME

def ask_name(update: Update, context: CallbackContext):
    name = update.message.text.strip()
    context.user_data.setdefault("order", {})
    context.user_data["order"]["customer_name"] = name
    update.message.reply_text(
        "📍 *Étape 2/3*\nIndiquez votre *adresse de livraison* :",
        parse_mode=ParseMode.MARKDOWN
    )
    return ASK_ADDRESS

def ask_address(update: Update, context: CallbackContext):
    address = update.message.text.strip()
    context.user_data["order"]["address"] = address
    update.message.reply_text(
        "📞 *Étape 3/3*\nVotre *numéro de téléphone* :",
        parse_mode=ParseMode.MARKDOWN
    )
    return ASK_PHONE

def ask_phone(update: Update, context: CallbackContext):
    phone = update.message.text.strip()
    context.user_data["order"]["phone"] = phone

    order = context.user_data["order"]
    cart = ensure_cart(context)

    recap_lines = [
        "🧾 *Récapitulatif commande*",
        f"👤 {order['customer_name']}",
        f"🏠 {order['address']}",
        f"📞 {order['phone']}",
        "",
    ]

    # détail panier
    for i in cart:
        label = i["name"] + (f" ({i['variant']})" if i.get("variant") else "")
        recap_lines.append(f"- {label} x{i['qty']} — {format_price(float(i['price']) * int(i['qty']))}")
    recap_lines.append(f"\n*Total* : {format_price(get_cart_total(cart))}")
    recap_lines.append("")
    recap_lines.append("Confirmez-vous la commande ?")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirmer", callback_data="confirm_order"),
         InlineKeyboardButton("❌ Annuler", callback_data="cancel_order")]
    ])
    update.message.reply_text("\n".join(recap_lines), parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    return ASK_CONFIRM

def confirm_or_cancel(update: Update, context: CallbackContext):
    query = update.callback_query
    data = query.data
    query.answer()

    if data == "cancel_order":
        context.user_data.pop("order", None)
        try:
            query.edit_message_text("❌ Commande annulée.", reply_markup=back_menu_kb())
        except BadRequest:
            context.bot.send_message(chat_id=query.message.chat_id, text="❌ Commande annulée.", reply_markup=back_menu_kb())
        return ConversationHandler.END

    if data == "confirm_order":
        order = context.user_data.get("order", {})
        cart = ensure_cart(context)

        # Construire le texte items avant de vider
        items_txt = ", ".join(f"{display_label(i)} x{i['qty']}" for i in cart)


        order["cart"] = cart
        order["total"] = round(get_cart_total(cart), 2)
        order["total_formatted"] = format_price(order["total"])
        order["user_id"] = query.from_user.id
        order["username"] = f"@{query.from_user.username}" if query.from_user.username else None
        order["created_at"] = datetime.utcnow().isoformat() + "Z"
        order["source"] = "bot"

        save_order(order)

        # Notif admin
        if ADMIN_CHAT_ID:
            try:
                text_admin = (
                    "📦 *Nouvelle commande*\n"
                    f"Client: {order['customer_name']} ({order.get('username')})\n"
                    f"Adresse: {order['address']}\n"
                    f"Téléphone: {order['phone']}\n"
                    f"Total: {order['total_formatted']}\n"
                    f"Articles: {items_txt}"
                )
                query.bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=text_admin, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass

        # Vider après envoi admin
        context.user_data["cart"] = []
        context.user_data.pop("order", None)

        try:
            query.edit_message_text(
                "✅ *Merci !* Votre commande a été enregistrée. Nous vous contacterons pour le paiement et la livraison.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=main_menu_kb()
            )
        except BadRequest:
            context.bot.send_message(chat_id=query.message.chat_id,
                                     text="✅ *Merci !* Votre commande a été enregistrée. Nous vous contacterons pour le paiement et la livraison.",
                                     parse_mode=ParseMode.MARKDOWN,
                                     reply_markup=main_menu_kb())
        return ConversationHandler.END

# =========================
# WebApp handler
# =========================
def handle_webapp_data(update: Update, context: CallbackContext):
    if not update.message or not update.message.web_app_data:
        return
    try:
        data_raw = update.message.web_app_data.data  # string JSON
        order_in = json.loads(data_raw)
        if order_in.get("kind") != "order":
            update.message.reply_text("Type de donnée non supporté.")
            return

        cart = order_in.get("cart", [])
        total = sum(float(i["price"]) * int(i["qty"]) for i in cart)
        order = {
            "customer_name": order_in.get("customer_name"),
            "address": order_in.get("address"),
            "phone": order_in.get("phone"),
            "cart": [
                {
                    "id": str(i["id"]),
                    "name": i["name"],
                    "price": float(i["price"]),
                    "qty": int(i["qty"]),
                    "variant": i.get("variant")
                } for i in cart
            ],
            "total": round(total, 2),
            "total_formatted": format_price(total),
            "user_id": update.effective_user.id,
            "username": f"@{update.effective_user.username}" if update.effective_user.username else None,
            "created_at": datetime.utcnow().isoformat() + "Z",
            "source": "webapp"
        }
        save_order(order)

        # Notif admin éventuelle
        if ADMIN_CHAT_ID:
            try:
                items = ", ".join(f"{display_label(i)} x{i['qty']}" for i in order.get("cart", []))

                text_admin = (
                    "📦 *Nouvelle commande (WebApp)*\n"
                    f"Client: {order.get('customer_name')} ({order.get('username')})\n"
                    f"Adresse: {order.get('address')}\n"
                    f"Téléphone: {order.get('phone')}\n"
                    f"Total: {order.get('total_formatted')}\n"
                    f"Articles: {items}"
                )
                context.bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=text_admin, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass

        update.message.reply_text("✅ Merci ! Votre commande (WebApp) a été enregistrée.")
    except Exception as e:
        print(f"[WebAppData error] {e}")
        update.message.reply_text("❌ Erreur lors du traitement de la commande.")

# =========================
# Error handler
# =========================
def error_handler(update, context):
    try:
        raise context.error
    except TelegramError as e:
        print(f"[TelegramError] {e}")
    except Exception as e:
        print(f"[Error] {e}")

# =========================
# Entrée principale
# =========================
def main():
    use_webhook = os.getenv("USE_WEBHOOK", "false").lower() == "true"
    port = int(os.getenv("PORT", "8080"))
    webhook_url = os.getenv("WEBHOOK_URL", "").strip()

    if not TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN manquant")

    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    # Commandes
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("help", help_cmd))
    dp.add_handler(CommandHandler("shop", shop_cmd))
    dp.add_handler(CommandHandler("cart", cart_cmd))

    # Handlers spécifiques AVANT le générique
    dp.add_handler(CallbackQueryHandler(add_to_cart_cb,      pattern=r"^add_\w+$"))
    dp.add_handler(CallbackQueryHandler(choose_variant_cb,   pattern=r"^choose_\w+_.+$"))
    dp.add_handler(CallbackQueryHandler(remove_from_cart_cb, pattern=r"^rm_.+"))

    # WebApp
    dp.add_handler(MessageHandler(has_webapp_data, handle_webapp_data))

    # Checkout conversation
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_checkout_cb, pattern=r"^checkout$")],
        states={
            ASK_NAME:    [MessageHandler(Filters.text & ~Filters.command, ask_name)],
            ASK_ADDRESS: [MessageHandler(Filters.text & ~Filters.command, ask_address)],
            ASK_PHONE:   [MessageHandler(Filters.text & ~Filters.command, ask_phone)],
            ASK_CONFIRM: [CallbackQueryHandler(confirm_or_cancel, pattern=r"^(confirm_order|cancel_order)$")]
        },
        fallbacks=[CommandHandler("start", start)],
        per_message=False
    )
    dp.add_handler(conv)

    # Handler générique de navigation (NE DOIT PAS matcher add_/choose_/rm_)
    dp.add_handler(CallbackQueryHandler(on_callback, pattern=r"^(shop|cart|clearcart|prod_\w+)$"))

    dp.add_error_handler(error_handler)

    if use_webhook:
        # Webhook (attention: ports autorisés côté Telegram: 80/88/443/8443)
        try:
            updater.bot.delete_webhook()
        except Exception:
            pass

        if not webhook_url:
            print("⚠️ WEBHOOK_URL manquant, bascule en polling.")
            start_health_server()
            updater.start_polling()
            print("🤖 Bot démarré en polling.")
            updater.idle()
            return

        path = TOKEN  # chemin secret
        updater.start_webhook(listen="0.0.0.0", port=port, url_path=path)
        updater.bot.set_webhook(url=f"{webhook_url.rstrip('/')}/{path}")
        print(f"🔗 Webhook démarré sur : {webhook_url.rstrip('/')}/{path}")
        updater.idle()
    else:
        # Polling + healthcheck HTTP pour Fly
        start_health_server()
        try:
            updater.bot.delete_webhook()
        except Exception:
            pass
        updater.start_polling()
        print("🤖 Bot démarré en polling.")
        updater.idle()

if __name__ == "__main__":
    main()
