import os
import json
from datetime import datetime
from typing import Dict, List, Optional

import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from dotenv import load_dotenv
from telegram.error import BadRequest, TelegramError
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ParseMode,
    WebAppInfo, KeyboardButton, ReplyKeyboardMarkup
)
from telegram.ext import (
    Updater, CommandHandler, CallbackQueryHandler, MessageHandler,
    Filters, CallbackContext, ConversationHandler, MessageFilter
)

# ==============
# Filtres & HC
# ==============
class HasWebAppData(MessageFilter):
    def filter(self, message):
        return bool(getattr(message, "web_app_data", None))

has_webapp_data = HasWebAppData()

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

# ======================
# Config & chargements
# ======================
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")  # Optionnel
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()

PRODUCTS_PATH = "products.json"
ORDERS_PATH = "orders.json"

# États de la conversation (checkout)
ASK_NAME, ASK_ADDRESS, ASK_PHONE, ASK_CONFIRM = range(4)

def load_products() -> List[Dict]:
    with open(PRODUCTS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

PRODUCTS = load_products()
PRODUCT_INDEX = {str(p["id"]): p for p in PRODUCTS}

def save_order(order: Dict):
    with open(ORDERS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(order, ensure_ascii=False) + "\n")

# ======================
# Utilitaires panier
# ======================
def format_price(v) -> str:
    return f"{float(v):.2f} €".replace(".", ",")

def ensure_cart(context: CallbackContext) -> List[Dict]:
    if "cart" not in context.user_data:
        context.user_data["cart"] = []
    return context.user_data["cart"]

def item_key(pid: str, variant: Optional[str]) -> str:
    return f"{pid}::{variant or ''}"

def product_by_id(pid: str) -> Optional[dict]:
    return PRODUCT_INDEX.get(str(pid))

def add_item_to_cart(context: CallbackContext, user_id: int, product: dict, variant: Optional[str] = None, qty: int = 1):
    cart = ensure_cart(context)
    key = item_key(str(product["id"]), variant)
    for it in cart:
        if it.get("key") == key:
            it["qty"] += qty
            break
    else:
        cart.append({
            "key": key,
            "id": str(product["id"]),
            "name": product["name"],
            "price": float(product["price"]),
            "image": product.get("image"),
            "variant": variant,
            "qty": qty
        })

def remove_item_from_cart_by_key(context: CallbackContext, key: str):
    cart = ensure_cart(context)
    context.user_data["cart"] = [it for it in cart if it.get("key") != key]

def get_cart_total(cart: List[Dict]) -> float:
    return sum(float(it["price"]) * int(it["qty"]) for it in cart)

def cart_count(context: CallbackContext) -> int:
    return sum(int(i.get("qty", 1)) for i in context.user_data.get("cart", []))

def cart_label(context: CallbackContext) -> str:
    count = cart_count(context)
    return f"🧺 Panier ({count})" if count else "🧺 Panier"

def cart_lines(context: CallbackContext) -> (str, List[List[InlineKeyboardButton]]):
    cart = context.user_data.get("cart", [])
    if not cart:
        return ("🧺 Panier vide.", [])
    total = 0.0
    lines = []
    buttons = []
    for it in cart:
        line_name = it["name"] + (f" — Taille {it['variant']}" if it.get("variant") else "")
        subtotal = float(it["price"]) * int(it["qty"])
        total += subtotal
        lines.append(f"• {line_name} x{it['qty']} — {format_price(subtotal)}")
        # bouton retirer item précis (clé = id::variant)
        buttons.append([InlineKeyboardButton(f"🗑️ Retirer {line_name}", callback_data=f"rm_{it['key']}")])
    text = "🧺 *Votre panier*\n\n" + "\n".join(lines) + f"\n\n*Total:* {format_price(total)}"
    return text, buttons

# ======================
# Claviers & menus
# ======================
def main_menu_kb(context: Optional[CallbackContext] = None):
    # Inline (fallback si pas de WebApp)
    rows = [[InlineKeyboardButton("🛍️ Voir les produits", callback_data="shop")],
            [InlineKeyboardButton(cart_label(context) if context else "🧺 Voir le panier", callback_data="cart")]]
    return InlineKeyboardMarkup(rows)

def back_menu_kb(context: Optional[CallbackContext] = None):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Retour boutique", callback_data="shop"),
         InlineKeyboardButton(cart_label(context) if context else "🧺 Panier", callback_data="cart")]
    ])

def product_kb(pid: str, context: CallbackContext) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Ajouter au panier", callback_data=f"add_{pid}")],
        [InlineKeyboardButton("⬅️ Retour boutique", callback_data="shop"),
         InlineKeyboardButton(cart_label(context), callback_data="cart")]
    ])

# ======================
# Commandes de base
# ======================
def start(update: Update, context: CallbackContext):
    buttons = []
    if WEBAPP_URL:
        buttons.append([KeyboardButton("🧾 Ouvrir la boutique", web_app=WebAppInfo(url=WEBAPP_URL))])
    reply_kb = ReplyKeyboardMarkup(buttons, resize_keyboard=True) if buttons else None

    update.message.reply_text(
        "👋 Bienvenue dans ma boutique Telegram !\n\n"
        "• 🛍️ Utilise les boutons ci-dessous\n"
        "• ou /shop pour la version bot",
        reply_markup=reply_kb or main_menu_kb(context)
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

# ======================
# Affichages
# ======================
def product_detail_text(p: Dict) -> str:
    lines = [f"*{p['name']}*", f"{format_price(p['price'])}", ""]
    if p.get("description"):
        lines.append(p["description"])
    return "\n".join(lines)

def send_product_list(update: Update, context: CallbackContext, query_msg=None):
    text = "*Produits disponibles*\nSélectionnez un article pour voir les détails."
    kb = []
    for p in PRODUCTS:
        kb.append([InlineKeyboardButton(f"{p['name']} — {format_price(p['price'])}",
                                        callback_data=f"prod_{p['id']}")])
    kb.append([InlineKeyboardButton(cart_label(context), callback_data="cart")])
    markup = InlineKeyboardMarkup(kb)

    if query_msg:
        try:
            if getattr(query_msg, "photo", None):
                # message précédent = photo -> on supprime et on renvoie du texte
                context.bot.delete_message(chat_id=query_msg.chat_id, message_id=query_msg.message_id)
                context.bot.send_message(
                    chat_id=query_msg.chat_id, text=text,
                    parse_mode=ParseMode.MARKDOWN, reply_markup=markup
                )
            else:
                query_msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
        except BadRequest:
            context.bot.send_message(
                chat_id=query_msg.chat_id, text=text,
                parse_mode=ParseMode.MARKDOWN, reply_markup=markup
            )
    else:
        update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)

def send_cart(update: Update, context: CallbackContext, query_msg=None):
    text, rm_buttons = cart_lines(context)
    kb_rows = rm_buttons[:]
    # actions
    cart = context.user_data.get("cart", [])
    action_row = []
    if cart:
        action_row.append(InlineKeyboardButton("✅ Passer commande", callback_data="checkout"))
        action_row.append(InlineKeyboardButton("🧹 Vider", callback_data="clearcart"))
    if action_row:
        kb_rows.append(action_row)
    kb_rows.append([InlineKeyboardButton("⬅️ Retour boutique", callback_data="shop")])
    markup = InlineKeyboardMarkup(kb_rows)

    if query_msg:
        try:
            if getattr(query_msg, "photo", None):
                context.bot.delete_message(chat_id=query_msg.chat_id, message_id=query_msg.message_id)
                context.bot.send_message(
                    chat_id=query_msg.chat_id, text=text,
                    parse_mode=ParseMode.MARKDOWN, reply_markup=markup
                )
            else:
                query_msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
        except BadRequest:
            context.bot.send_message(
                chat_id=query_msg.chat_id, text=text,
                parse_mode=ParseMode.MARKDOWN, reply_markup=markup
            )
    else:
        update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)

# ======================
# Callbacks inline
# ======================
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
            query.edit_message_text("🧺 Panier vidé.", reply_markup=back_menu_kb(context))
        except BadRequest:
            context.bot.send_message(chat_id=query.message.chat_id, text="🧺 Panier vidé.",
                                     reply_markup=back_menu_kb(context))
        return

    if data.startswith("prod_"):
        pid = data.split("_", 1)[1]
        p = PRODUCT_INDEX.get(pid)
        if not p:
            query.edit_message_text("❌ Produit introuvable.", reply_markup=back_menu_kb(context))
            return

        kb = product_kb(pid, context)
        image_url = p.get("image")
        caption = product_detail_text(p)
        if image_url:
            try:
                # on remplace le message précédent par une photo
                try:
                    query.message.delete()
                except Exception:
                    pass
                context.bot.send_photo(
                    chat_id=query.message.chat_id,
                    photo=image_url,
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=kb
                )
            except Exception:
                # fallback texte
                query.edit_message_text(caption, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        else:
            query.edit_message_text(caption, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return

    if data.startswith("rm_"):
        key = data.split("_", 1)[1]
        remove_item_from_cart_by_key(context, key)
        send_cart(update, context, query_msg=query.message)
        return

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
        # Proposer un choix de taille
        kb = [[InlineKeyboardButton(v, callback_data=f"choose_{pid}_{v}")] for v in variants]
        kb.append([InlineKeyboardButton("⬅️ Retour", callback_data=f"prod_{pid}")])
        query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(kb))
        query.answer("Choisis une taille")
        return

    # pas de variants → on ajoute direct
    add_item_to_cart(context, update.effective_user.id, prod, variant=None, qty=1)

    # Met à jour uniquement les boutons (avec compteur panier)
    try:
        query.edit_message_reply_markup(reply_markup=product_kb(pid, context))
    except Exception:
        pass

    query.answer("Ajouté au panier ✅", show_alert=False)

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

    add_item_to_cart(context, update.effective_user.id, prod, variant=variant, qty=1)

    # Met à jour les boutons de la fiche produit (avec compteur)
    try:
        query.edit_message_reply_markup(reply_markup=product_kb(pid, context))
    except Exception:
        pass

    query.answer(f"Ajouté ({variant}) ✅", show_alert=False)

# ======================
# Checkout (Conversation)
# ======================
def start_checkout_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()

    cart = ensure_cart(context)
    if not cart:
        query.answer("Votre panier est vide.", show_alert=True)
        return ConversationHandler.END

    # Prépare la commande (on copie le panier actuel)
    context.user_data["order"] = {"cart": list(cart)}
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
    cart = order.get("cart", []) or ensure_cart(context)
    total = format_price(get_cart_total(cart))

    # Récap + demandes confirmation
    recap_lines = [
        "🧾 *Récapitulatif commande*",
        f"👤 {order.get('customer_name')}",
        f"🏠 {order.get('address')}",
        f"📞 {order.get('phone')}",
        "",
    ]
    # Détails panier
    for it in cart:
        line_name = it["name"] + (f" — Taille {it['variant']}" if it.get("variant") else "")
        recap_lines.append(f"• {line_name} x{it['qty']} — {format_price(float(it['price']) * int(it['qty']))}")
    recap_lines.append(f"\n*Total*: {total}\n")
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
            query.edit_message_text("❌ Commande annulée.", reply_markup=back_menu_kb(context))
        except BadRequest:
            context.bot.send_message(chat_id=query.message.chat_id, text="❌ Commande annulée.",
                                     reply_markup=back_menu_kb(context))
        return ConversationHandler.END

    if data == "confirm_order":
        # Construire l'ordre final à partir du draft
        order = context.user_data.get("order", {})
        cart_copy = list(order.get("cart", []) or ensure_cart(context))
        total_val = round(get_cart_total(cart_copy), 2)
        order_final = {
            "customer_name": order.get("customer_name"),
            "address": order.get("address"),
            "phone": order.get("phone"),
            "cart": cart_copy,
            "total": total_val,
            "total_formatted": format_price(total_val),
            "user_id": query.from_user.id,
            "username": f"@{query.from_user.username}" if query.from_user.username else None,
            "created_at": datetime.utcnow().isoformat() + "Z",
            "source": "bot"
        }

        save_order(order_final)

        # Notifier admin avant de nettoyer le panier
        if ADMIN_CHAT_ID:
            try:
                items_txt = ", ".join(
                    f"{i['name']}{(' (' + str(i['variant']) + ')') if i.get('variant') else ''} x{int(i['qty'])}"
                    for i in cart_copy
                )
                text_admin = (
                    "📦 *Nouvelle commande*\n"
                    f"Client: {order_final['customer_name']} ({order_final.get('username')})\n"
                    f"Adresse: {order_final['address']}\n"
                    f"Téléphone: {order_final['phone']}\n"
                    f"Total: {order_final['total_formatted']}\n"
                    f"Articles: {items_txt}"
                )
                query.bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=text_admin, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass

        # Nettoyage
        context.user_data["cart"] = []
        context.user_data.pop("order", None)

        try:
            query.edit_message_text(
                "✅ *Merci !* Votre commande a été enregistrée. Nous vous contacterons pour le paiement et la livraison.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=main_menu_kb(context)
            )
        except BadRequest:
            context.bot.send_message(
                chat_id=query.message.chat_id,
                text="✅ *Merci !* Votre commande a été enregistrée. Nous vous contacterons pour le paiement et la livraison.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=main_menu_kb(context)
            )

        return ConversationHandler.END

# ======================
# WebApp (checkout via mini-app)
# ======================
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
            "cart": cart,
            "total": round(total, 2),
            "total_formatted": format_price(total),
            "user_id": update.effective_user.id,
            "username": f"@{update.effective_user.username}" if update.effective_user.username else None,
            "created_at": datetime.utcnow().isoformat() + "Z",
            "source": "webapp"
        }
        save_order(order)

        if ADMIN_CHAT_ID:
            try:
                items_txt = ", ".join(
                    f"{i['name']}{(' (' + str(i.get('variant')) + ')') if i.get('variant') else ''} x{int(i['qty'])}"
                    for i in cart
                )
                text_admin = (
                    "📦 *Nouvelle commande (WebApp)*\n"
                    f"Client: {order.get('customer_name')} ({order.get('username')})\n"
                    f"Adresse: {order.get('address')}\n"
                    f"Téléphone: {order.get('phone')}\n"
                    f"Total: {order.get('total_formatted')}\n"
                    f"Articles: {items_txt}"
                )
                context.bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=text_admin, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass

        update.message.reply_text("✅ Merci ! Votre commande (WebApp) a été enregistrée.")
    except Exception as e:
        print(f"[WebAppData error] {e}")
        update.message.reply_text("❌ Erreur lors du traitement de la commande.")

# ======================
# Errors & Main
# ======================
def error_handler(update, context):
    try:
        raise context.error
    except TelegramError as e:
        print(f"[TelegramError] {e}")
    except Exception as e:
        print(f"[Error] {e}")

def main():
    use_webhook = os.getenv("USE_WEBHOOK", "false").lower() == "true"
    port = int(os.getenv("PORT", "8080"))
    webhook_url = os.getenv("WEBHOOK_URL", "").strip()

    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    # Commandes
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("help", help_cmd))
    dp.add_handler(CommandHandler("shop", shop_cmd))
    dp.add_handler(CommandHandler("cart", cart_cmd))

    # Données Mini-App
    dp.add_handler(MessageHandler(has_webapp_data, handle_webapp_data))

    # Callbacks spécifiques
    dp.add_handler(CallbackQueryHandler(add_to_cart_cb, pattern=r"^add_\w+$"))
    dp.add_handler(CallbackQueryHandler(choose_variant_cb, pattern=r"^choose_\w+_.+$"))
    dp.add_handler(CallbackQueryHandler(on_callback, pattern=r"^(shop|cart|clearcart|prod_\w+|rm_.+)$"))

    # Conversation Checkout (placer AVANT/au-dessus de tout handler fourre-tout si nécessaire)
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_checkout_cb, pattern=r"^checkout$")],
        states={
            ASK_NAME:    [MessageHandler(Filters.text & ~Filters.command, ask_name)],
            ASK_ADDRESS: [MessageHandler(Filters.text & ~Filters.command, ask_address)],
            ASK_PHONE:   [MessageHandler(Filters.text & ~Filters.command, ask_phone)],
            ASK_CONFIRM: [CallbackQueryHandler(confirm_or_cancel, pattern=r"^(confirm_order|cancel_order)$")]
        },
        fallbacks=[CommandHandler("start", start)]
    )
    dp.add_handler(conv)

    dp.add_error_handler(error_handler)

    if use_webhook:
        listen_addr = "0.0.0.0"
        path = TOKEN  # chemin "secret"

        updater.start_webhook(listen=listen_addr, port=port, url_path=path)
        try:
            updater.bot.delete_webhook()
        except Exception:
            pass

        if webhook_url:
            webhook_url = webhook_url.rstrip("/")
            updater.bot.set_webhook(url=f"{webhook_url}/{path}")
            print(f"🔗 Webhook démarré sur : {webhook_url}/{path}")
        else:
            print("⚠️ WEBHOOK_URL manquant.")
        updater.idle()
    else:
        # Healthcheck HTTP pour Fly
        start_health_server()

        # S'assurer qu'aucun webhook n'est actif
        try:
            updater.bot.delete_webhook()
        except Exception:
            pass

        updater.start_polling()
        print("🤖 Bot démarré en polling.")
        updater.idle()

if __name__ == "__main__":
    main()
