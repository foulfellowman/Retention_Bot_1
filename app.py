# app.py
from flask import Flask, request, render_template, session, redirect, url_for, current_app
import os
import psutil
from dotenv import load_dotenv

from admin import Admin
from db import DB, insert_message
from gpt import GPTClient
from twilio_test import TwilioSMSClient
from user_context import UserContext


# ----------------------------
# App factory & wiring
# ----------------------------

def create_app() -> Flask:
    load_dotenv()
    app = Flask(__name__)
    app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret")

    # Build dependencies once per process; store on app config
    services = {
        "gpt": GPTClient(
            max_tokens=int(os.getenv("max_tokens"))
        ),
        "twilio": TwilioSMSClient(
            os.getenv("TWILIO_SID"),
            os.getenv("TWILIO_TOKEN"),
            os.getenv("TWILIO_MESSAGINGID"),
        ),
        "admin_user": Admin(
            username=os.getenv("ADMIN_USERNAME", "admin"),
            password=os.getenv("ADMIN_PASSWORD", "pass123"),
        ),
    }
    app.config["services"] = services

    # ----------------------------
    # Helpers (no globals)
    # ----------------------------
    def get_services():
        return current_app.config["services"]

    def get_current_user():
        if session.get("logged_in"):
            return get_services()["admin_user"]
        return None

    def get_twilio_sid():
        sid = request.form.get("MessageSid")
        return sid if sid else None

    def print_memory_usage():
        process = psutil.Process(os.getpid())
        mem = process.memory_info().rss / 1024 / 1024  # MB
        print(f"Memory usage: {mem:.2f} MB")

    # ----------------------------
    # Routes
    # ----------------------------
    @app.route("/login", methods=["GET", "POST"])
    def login():
        admin_user = get_services()["admin_user"]
        if request.method == "POST":
            username = request.form.get("username")
            password = request.form.get("password")
            if username == admin_user.username and admin_user.check_password(password):
                session["logged_in"] = True
                return redirect(url_for("settings"))
            return "Invalid credentials", 401
        return '''
            <form method="post">
                Username: <input name="username"><br>
                Password: <input name="password" type="password"><br>
                <button type="submit">Login</button>
            </form>
        '''

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/ping")
    def ping():
        # avoid emoji per request
        return "<span style='color: green;'>Server is up</span>"

    @app.route("/settings")
    def settings():
        user = get_current_user()
        return render_template("partials/settings.html", user=user)

    @app.route("/settings-modal")
    def settings_modal():
        user = get_current_user()
        return render_template("partials/settings_modal.html", user=user)

    @app.route("/save-settings", methods=["POST"])
    def save_settings():
        user = get_current_user()
        if user:
            user.update_settings(
                request.form.get("api-key"),
                request.form.get("twilio-sid"),
                request.form.get("twilio-token"),
            )
        return render_template("partials/settings.html", user=user)

    @app.route("/sms", methods=["POST"])
    def sms_reply():
        # Inbound webhook from Twilio
        incoming_msg = (request.form.get("Body") or "").strip()
        from_number = request.form.get("From")
        twilio_sid = get_twilio_sid()

        print(f"Incoming from {from_number}: {incoming_msg}")

        # Build request-scoped objects
        db = DB()
        try:
            # Record inbound
            insert_message(db, from_number, incoming_msg, twilio_sid)

            # Build a proper UserContext (fixes the previous string misuse)
            user_ctx = UserContext(str(from_number))
            # Optionally, prime GPT context with current user info on first contact
            # user_ctx.set_user_info("Name", ["Service A", "Service B"], 90, "Service A")
            # user_ctx.turn_into_gpt_context(incoming_sms=incoming_msg) -> if needed:
            # get_services()["gpt"].set_context(user_ctx.turn_into_gpt_context(incoming_sms=incoming_msg))

            # Generate reply via GPT
            gpt = get_services()["gpt"]
            reply = gpt.generate_response(incoming_msg, user_ctx, db)

            # Send reply out-of-band via Twilio REST
            twilio_client = get_services()["twilio"]
            twilio_client.send_sms(to_phone=from_number, message=reply)

        finally:
            try:
                db.close()
            except Exception:
                pass

        # Return a simple 200 OK to Twilio (we already replied via REST)
        return "OK", 200

    @app.route('/')
    def index():
        user = get_current_user()
        db = DB()
        try:
            conversations = db.fetch_conversations(q=request.args.get('q'))
            selected_phone = conversations[0]['phone_number'] if conversations else None
            messages = db.SQL_full_conversation_per_phone(selected_phone) if selected_phone else []
            return render_template(
                'index.html',
                conversations=conversations,
                selected_phone=selected_phone,
                conversations_count=len(conversations),
                messages=messages,
                user_is_logged_in=bool(user),
            )
        finally:
            db.close()

    @app.route('/conversations')
    def conversations_list():
        q = request.args.get('q')
        db = DB()
        try:
            conversations = db.fetch_conversations(q=q if q else None)
            # Either render a partial tbody, or return JSON
            return render_template('partials/conversations_tbody_inner.html', conversations=conversations,
                                   conversations_count=len(conversations))
        finally:
            db.close()

    @app.route('/conversations/<phone>')
    def conversation_view(phone):
        db = DB()
        try:
            messages = db.SQL_full_conversation_per_phone(phone)
            conversations = db.fetch_conversations()
            # print(conversations)
            print('Selected: ', phone)

            return render_template(
                'partials/conversations_list_response.html',
                selected_phone=phone,
                messages=messages,
                conversations=conversations,
                conversations_count=len(conversations))
        finally:
            db.close()

    @app.route('/conversations/<phone>/export')
    def conversation_export(phone):
        db = DB()
        try:
            messages = db.SQL_full_conversation_per_phone(phone)
            lines = [f"[{m['sent_at']:%Y-%m-%d %H:%M}] {m['role']}: {m['content']}"
                     for m in messages]
            txt = "\n".join(lines)
            return current_app.response_class(
                txt, mimetype='text/plain',
                headers={'Content-Disposition': f'attachment; filename={phone}.txt'}
            )
        finally:
            db.close()

    return app


# ----------------------------
# Dev entrypoint
# ----------------------------
if __name__ == "__main__":
    app = create_app()
    app.run(debug=True)
