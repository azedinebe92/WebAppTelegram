import os
import json
from datetime import datetime
from typing import Dict, List, Optional
from telegram.error import BadRequest
from telegram.error import TelegramError


from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ParseMode, InputMediaPhoto
)
from telegram.ext import (
    Updater, CommandHandler, CallbackQueryHandler, MessageHandler,
    Filters, CallbackContext, ConversationHandler
)

# --------------------
# Config / Chargement
# --------------------
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")  # Optionnel: pour notification admin

PRODUCTS_PATH = "products.json"
ORDERS_PATH = "orders.json"

# États pour le checkout
ASK_NAME, ASK_ADDRESS, ASK_PHONE, ASK_CONFIRM = range(4)

# Mémoire simple : produits chargés en mémoire
def load_products() -> List[Dict]:
    with open(PRODUCTS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

PRODUCTS = load_products()
PRODUCT_INDEX = {str(p["id"]): p for p in PRODUCTS}

def save_order(order: Dict):
    # Append JSON line dans orders.json pour simplicité/robustesse
    with open(ORDERS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(order, ensure_ascii=False) + "\n")

# --------------------
# Utilitaires
# --------------------
def format_price(v) -> str:
    # Format euro simple
    return f"{float(v):.2f} €".replace(".", ",")

def get_cart_total(cart: List[Dict]) -> float:
    return sum(item["price"] * item["qty"] for item in cart)

def ensure_cart(context: CallbackContext) -> List[Dict]:
    if "cart" not in context.user_data:
        context.user_data["cart"] = []
    return context.user_data["cart"]

def add_to_cart(context: CallbackContext, product_id: str, qty: int = 1):
    cart = ensure_cart(context)
    # Regrouper par produit
    for item in cart:
        if item["id"] == product_id:
            item["qty"] += qty
            return
    p = PRODUCT_INDEX[product_id]
    cart.append({
        "id": product_id,
        "name": p["name"],
        "price": float(p["price"]),
        "qty": qty
    })

def remove_from_cart(context: CallbackContext, product_id: str):
    cart = ensure_cart(context)
    context.user_data["cart"] = [i for i in cart if i["id"] != product_id]

def cart_text(cart: List[Dict]) -> str:
    if not cart:
        return "🧺 Votre panier est vide."
    lines = ["🧺 *Votre panier*"]
    for i in cart:
        lines.append(f"- {i['name']} x{i['qty']} — {format_price(i['price'] * i['qty'])}")
    lines.append(f"\n*Total*: {format_price(get_cart_total(cart))}")
    return "\n".join(lines)

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

# --------------------
# Commandes de base
# --------------------
def start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "👋 Bienvenue dans ma boutique Telegram !\n\n"
        "Utilisez les boutons ci-dessous pour commencer.",
        reply_markup=main_menu_kb()
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
    cart = ensure_cart(context)
    kb_rows = []
    # Boutons pour retirer des items
    for i in cart:
        kb_rows.append([InlineKeyboardButton(f"🗑️ Retirer {i['name']}", callback_data=f"rm_{i['id']}")])
    # Actions
    action_row = []
    if cart:
        action_row.append(InlineKeyboardButton("✅ Passer commande", callback_data="checkout"))
        action_row.append(InlineKeyboardButton("🧹 Vider", callback_data="clearcart"))
    if action_row:
        kb_rows.append(action_row)
    kb_rows.append([InlineKeyboardButton("⬅️ Retour boutique", callback_data="shop")])

    update.message.reply_text(
        cart_text(cart),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb_rows)
    )

# --------------------
# Liste & Fiche produit
# --------------------
def send_product_list(update: Update, context: CallbackContext, query_msg=None):
    text = "*Produits disponibles*\nSélectionnez un article pour voir les détails."
    kb = []
    for p in PRODUCTS:
        kb.append([InlineKeyboardButton(f"{p['name']} — {format_price(p['price'])}",
                                        callback_data=f"prod_{p['id']}")])
    kb.append([InlineKeyboardButton("🧺 Panier", callback_data="cart")])
    markup = InlineKeyboardMarkup(kb)

    # Si on vient d’un bouton (CallbackQuery)
    if query_msg:
        try:
            # 👇 Si le message est une photo, on ne peut pas faire edit_text
            if getattr(query_msg, "photo", None):
                # on supprime la photo et on renvoie un nouveau message texte
                context.bot.delete_message(chat_id=query_msg.chat_id, message_id=query_msg.message_id)
                context.bot.send_message(
                    chat_id=query_msg.chat_id,
                    text=text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=markup
                )
            else:
                # Sinon, c’est un message texte : on peut l’éditer
                query_msg.edit_caption(caption=text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
        except BadRequest:
            # Fallback robuste: on renvoie un nouveau message
            context.bot.send_message(
                chat_id=query_msg.chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=markup
            )
    else:
        # Appel depuis /shop ou /start
        update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)


def product_detail_text(p: Dict) -> str:
    lines = [f"*{p['name']}*", f"{format_price(p['price'])}", "", p.get("description", "")]
    return "\n".join(lines)

def on_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    data = query.data
    query.answer()

    # Navigation boutique
    if data == "shop":
        send_product_list(update, context, query_msg=query.message)
        return

    if data == "cart":
        cart = ensure_cart(context)
        kb_rows = []
        for i in cart:
            kb_rows.append([InlineKeyboardButton(f"🗑️ Retirer {i['name']}", callback_data=f"rm_{i['id']}")])
        action_row = []
        if cart:
            action_row.append(InlineKeyboardButton("✅ Passer commande", callback_data="checkout"))
            action_row.append(InlineKeyboardButton("🧹 Vider", callback_data="clearcart"))
        if action_row:
            kb_rows.append(action_row)
        kb_rows.append([InlineKeyboardButton("⬅️ Retour boutique", callback_data="shop")])
        markup = InlineKeyboardMarkup(kb_rows)
        text = cart_text(cart)

        try:
            # Si on vient d'une fiche photo, on ne peut pas edit_text => on supprime et on renvoie un nouveau message
            if getattr(query.message, "photo", None):
                context.bot.delete_message(chat_id=query.message.chat_id, message_id=query.message.message_id)
                context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=markup
                )
            else:
                query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
        except BadRequest:
            # Fallback robuste
            context.bot.send_message(
                chat_id=query.message.chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=markup
            )
        return


    if data.startswith("prod_"):
        pid = data.split("_", 1)[1]
        p = PRODUCT_INDEX.get(pid)
        if not p:
            query.edit_message_text("❌ Produit introuvable.", reply_markup=back_menu_kb())
            return

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Ajouter au panier", callback_data=f"add_{pid}")],
            [InlineKeyboardButton("⬅️ Retour boutique", callback_data="shop"),
             InlineKeyboardButton("🧺 Panier", callback_data="cart")]
        ])

        # Afficher image si disponible
        image_url = p.get("image")
        caption = product_detail_text(p)
        if image_url:
            try:
                # Si message précédent est texte, on envoie une nouvelle photo
                query.message.delete()
                context.bot.send_photo(
                    chat_id=query.message.chat_id,
                    photo=image_url,
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=kb
                )
            except Exception:
                # fallback en texte si l'image échoue
                query.edit_message_text(caption, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        else:
            query.edit_message_text(caption, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return

    #if data.startswith("add_"):
    #    pid = data.split("_", 1)[1]
    #    if pid not in PRODUCT_INDEX:
    #        query.answer("Produit introuvable.", show_alert=True)
    #        return
    #    add_to_cart(context, pid, qty=1)
    #    query.answer("Ajouté au panier ✅")
    #    return

    if data.startswith("rm_"):
        pid = data.split("_", 1)[1]
        remove_from_cart(context, pid)
        # Réactualiser le panier
        cart = ensure_cart(context)
        kb_rows = []
        for i in cart:
            kb_rows.append([InlineKeyboardButton(f"🗑️ Retirer {i['name']}", callback_data=f"rm_{i['id']}")])
        action_row = []
        if cart:
            action_row.append(InlineKeyboardButton("✅ Passer commande", callback_data="checkout"))
            action_row.append(InlineKeyboardButton("🧹 Vider", callback_data="clearcart"))
        if action_row:
            kb_rows.append(action_row)
        kb_rows.append([InlineKeyboardButton("⬅️ Retour boutique", callback_data="shop")])
        query.edit_message_text(
            cart_text(cart),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb_rows)
        )
        return

    if data == "clearcart":
        context.user_data["cart"] = []
        query.edit_message_text("🧺 Panier vidé.", reply_markup=back_menu_kb())
        return

    if data == "checkout":
        cart = ensure_cart(context)
        if not cart:
            query.answer("Votre panier est vide.", show_alert=True)
            return
        context.user_data["order"] = {"cart": cart}
        query.edit_message_text(
            "📝 *Commande — Étape 1/3*\n\nQuel est votre *nom complet* ?",
            parse_mode=ParseMode.MARKDOWN
        )
        return ASK_NAME


def start_checkout_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()

    cart = ensure_cart(context)
    if not cart:
        query.answer("Votre panier est vide.", show_alert=True)
        return ConversationHandler.END

    # Prépare la commande
    context.user_data["order"] = {"cart": cart}
    query.edit_message_text(
        "📝 *Commande — Étape 1/3*\n\nQuel est votre *nom complet* ?",
        parse_mode=ParseMode.MARKDOWN
    )
    return ASK_NAME



def add_to_cart_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    data = query.data  # ex: "add_1"
    pid = data.split("_", 1)[1]

    if pid not in PRODUCT_INDEX:
        query.answer("Produit introuvable.", show_alert=True)
        return

    # Ajouter au panier
    add_to_cart(context, pid, qty=1)
    query.answer("Ajouté au panier ✅")

    # Recomposer le clavier de la fiche produit pour que l'utilisateur voie qu'il s'est passé qqch
    p = PRODUCT_INDEX[pid]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Ajouter encore", callback_data=f"add_{pid}")],
        [InlineKeyboardButton("🧺 Aller au panier", callback_data="cart"),
         InlineKeyboardButton("⬅️ Retour boutique", callback_data="shop")]
    ])

    # Si la fiche est une photo (avec légende), on édite la légende. Sinon, on édite le texte.
    try:
        if getattr(query.message, "photo", None):
            # APRÈS (v13 : on édite le Message)
            if getattr(query.message, "photo", None):
                query.message.edit_caption(
                    caption=product_detail_text(p),
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=kb
                )
            else:
                query.edit_message_text(
                    product_detail_text(p),
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=kb
                )

        else:
            query.edit_message_text(
                product_detail_text(p),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb
            )
    except BadRequest:
        # Si l'édition échoue (cas edge), on renvoie un nouveau message propre
        context.bot.send_message(
            chat_id=query.message.chat_id,
            text=product_detail_text(p),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb
        )


# --------------------
# Checkout (Conversation)
# --------------------
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
    total = format_price(get_cart_total(cart))

    recap_lines = [
        "🧾 *Récapitulatif commande*",
        f"👤 {order['customer_name']}",
        f"🏠 {order['address']}",
        f"📞 {order['phone']}",
        "",
        cart_text(cart),
        "",
        "Confirmez-vous la commande ?"
    ]
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
        # Réinitialiser la commande mais garder le panier
        context.user_data.pop("order", None)
        query.edit_message_text("❌ Commande annulée.", reply_markup=back_menu_kb())
        return ConversationHandler.END

    if data == "confirm_order":
        # Construire et sauvegarder la commande
        order = context.user_data.get("order", {})
        cart = ensure_cart(context)
        order["cart"] = cart
        order["total"] = round(get_cart_total(cart), 2)
        order["total_formatted"] = format_price(order["total"])
        order["user_id"] = query.from_user.id
        order["username"] = f"@{query.from_user.username}" if query.from_user.username else None
        order["created_at"] = datetime.utcnow().isoformat() + "Z"

        save_order(order)
        context.user_data["cart"] = []  # vider panier
        context.user_data.pop("order", None)

        # Notifier admin si configuré
        if ADMIN_CHAT_ID:
            try:
                text_admin = (
                    "📦 *Nouvelle commande*\n"
                    f"Client: {order.get('customer_name')} ({order.get('username')})\n"
                    f"Adresse: {order.get('address')}\n"
                    f"Téléphone: {order.get('phone')}\n"
                    f"Total: {order.get('total_formatted')}\n"
                    f"Articles: " + ", ".join(f"{i['name']} x{i['qty']}" for i in order["cart"])
                )
                query.bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=text_admin, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass

        query.edit_message_text(
            "✅ *Merci !* Votre commande a été enregistrée. Nous vous contacterons pour le paiement et la livraison.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_kb()
        )
        return ConversationHandler.END
    

def error_handler(update, context):
    try:
        raise context.error
    except TelegramError as e:
        print(f"[TelegramError] {e}")
    except Exception as e:
        print(f"[Error] {e}")

# --------------------
# Entrée / wiring
# --------------------
def main():
    use_webhook = os.getenv("USE_WEBHOOK", "false").lower() == "true"
    port = int(os.getenv("PORT", "8080"))
    webhook_url = os.getenv("WEBHOOK_URL", "").strip()

    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    # === Handlers (comme tu les as déjà) ===
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("help", help_cmd))
    dp.add_handler(CommandHandler("shop", shop_cmd))
    dp.add_handler(CommandHandler("cart", cart_cmd))

    dp.add_handler(CallbackQueryHandler(add_to_cart_cb, pattern=r"^add_\w+$"))

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_checkout_cb, pattern=r"^checkout$")],
        states={
            ASK_NAME: [MessageHandler(Filters.text & ~Filters.command, ask_name)],
            ASK_ADDRESS: [MessageHandler(Filters.text & ~Filters.command, ask_address)],
            ASK_PHONE: [MessageHandler(Filters.text & ~Filters.command, ask_phone)],
            ASK_CONFIRM: [CallbackQueryHandler(confirm_or_cancel, pattern=r"^(confirm_order|cancel_order)$")]
        },
        fallbacks=[CommandHandler("start", start)]
    )
    dp.add_handler(conv)

    dp.add_handler(CallbackQueryHandler(
        on_callback,
        pattern=r"^(shop|cart|clearcart|prod_\w+|rm_\w+)$"
    ))

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
        # ✅ IMPORTANT pour le polling : enlever tout webhook résiduel
        try:
            updater.bot.delete_webhook()
        except Exception:
            pass

        updater.start_polling()
        print("🤖 Bot démarré en polling.")
        updater.idle()



if __name__ == "__main__":
    main()
