import json
# app.py
from flask import Flask, request, render_template, redirect, url_for, current_app, make_response, jsonify
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from flask_wtf import FlaskForm
from wtforms import PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired
import os
import psutil
from dotenv import load_dotenv

from admin import Admin
from db import DB, insert_message
from gpt import GPTClient
from twilio_test import TwilioSMSClient
from user_context import UserContext


class LoginForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired()])
    password = PasswordField('Password', validators=[DataRequired()])
    submit = SubmitField('Log In')


# ----------------------------
# App factory & wiring
# ----------------------------

def create_app() -> Flask:
    load_dotenv()
    app = Flask(__name__)
    app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret")

    login_manager = LoginManager()
    login_manager.login_view = "login"
    login_manager.init_app(app)

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

    @login_manager.user_loader
    def load_user(user_id: str):
        admin_user = get_services()["admin_user"]
        if admin_user.get_id() == user_id:
            return admin_user
        return None

    def get_current_user():
        if current_user.is_authenticated:
            return current_user
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
        if current_user.is_authenticated:
            return redirect(url_for("settings"))

        form = LoginForm()
        if form.validate_on_submit():
            admin_user = get_services()["admin_user"]
            if form.username.data == admin_user.username and admin_user.check_password(form.password.data):
                login_user(admin_user)
                next_url = request.args.get("next")
                if next_url and not next_url.startswith("/"):
                    next_url = None
                return redirect(next_url or url_for("index"))
            form.password.errors.append("Invalid username or password.")
        return render_template("login.html", form=form)

    @app.route("/logout", methods=["POST"])
    @login_required
    def logout():
        logout_user()
        return redirect(url_for("login"))

    @app.route("/ping")
    def ping():
        # avoid emoji per request
        return "<span style='color: green;'>Server is up</span>"

    @app.route("/settings")
    @login_required
    def settings():
        user = get_current_user()
        return render_template("partials/settings.html", user=user)

    @app.route("/settings-modal")
    @login_required
    def settings_modal():
        user = get_current_user()
        return render_template("partials/settings_modal.html", user=user)

    @app.route("/save-settings", methods=["POST"])
    @login_required
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
            # get_services()["gpt"].set_context(user_ctx.phone_number, user_ctx.turn_into_gpt_context(incoming_sms=incoming_msg))

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
    @login_required
    def index():
        user = get_current_user()
        q = request.args.get('q')
        sort = request.args.get('sort')
        direction = request.args.get('direction') or 'asc'
        db = DB()
        try:
            conversations = db.fetch_conversations(q=q, sort=sort, direction=direction)
            selected_phone = conversations[0]['phone_number'] if conversations else None
            messages = db.SQL_full_conversation_per_phone(selected_phone) if selected_phone else []
            return render_template(
                'index.html',
                conversations=conversations,
                selected_phone=selected_phone,
                conversations_count=len(conversations),
                messages=messages,
                user_is_logged_in=bool(user),
                current_sort=(sort or ''),
                current_direction=direction,
                current_query=(q or ''),
            )
        finally:
            db.close()

    @app.route('/conversations')
    @login_required
    def conversations_list():
        q = request.args.get('q')
        sort = request.args.get('sort')
        direction = request.args.get('direction') or 'asc'
        selected_phone = request.args.get('selected_phone')
        db = DB()
        try:
            conversations = db.fetch_conversations(q=q if q else None, sort=sort, direction=direction)
            # Either render a partial tbody, or return JSON
            return render_template('partials/conversations_tbody_inner.html',
                                   conversations=conversations,
                                   conversations_count=len(conversations),
                                    selected_phone=selected_phone,
                                   current_sort=(sort or ''),
                                   current_direction=direction)
        finally:
            db.close()

    @app.route('/conversations/<phone>')
    @login_required
    def conversation_view(phone):
        direction = request.args.get('direction') or 'asc'
        db = DB()
        try:
            messages = db.SQL_full_conversation_per_phone(phone)
            q = request.args.get('q')
            sort = request.args.get('sort')
            conversations = db.fetch_conversations(q=q if q else None, sort=sort, direction=direction)
            # print(conversations)
            print('Selected: ', phone)

            return render_template(
                'partials/conversations_list_response.html',
                selected_phone=phone,
                messages=messages,
                conversations=conversations,
                conversations_count=len(conversations),
                current_sort=(sort or ''),
                current_direction=direction)
        finally:
            db.close()

    @app.route('/conversations/<phone>/edit', methods=['GET', 'POST'])
    @login_required
    def conversation_edit(phone):
        allowed_states = ['start', 'interested', 'action_sqft', 'confused', 'not_interested', 'follow_up', 'pause', 'stop', 'done']
        db = DB()

        def fetch_current_state():
            cur = db.conn.cursor()
            cur.execute(
                'SELECT statename FROM public.fsm_state WHERE phone_number = %s',
                (phone,)
            )
            row = cur.fetchone()
            cur.close()
            return row[0] if row and row[0] else 'start'

        try:
            if request.method == 'POST':
                new_state = (request.form.get('state') or '').strip()
                if new_state not in allowed_states:
                    current_state = fetch_current_state()
                    return render_template(
                        'partials/conversation_edit_modal.html',
                        phone=phone,
                        current_state=current_state,
                        states=allowed_states,
                        error='Invalid state selection. Please choose a valid state.'
                    )

                cur = db.conn.cursor()
                cur.execute(
                    """
                    UPDATE public.fsm_state
                    SET statename = %s
                    WHERE phone_number = %s
                    """,
                    (new_state, phone)
                )
                if cur.rowcount == 0:
                    cur.execute(
                        """
                        INSERT INTO public.fsm_state (phone_number, statename)
                        VALUES (%s, %s)
                        """,
                        (phone, new_state)
                    )
                db.conn.commit()
                cur.close()

                response = make_response('', 204)
                response.headers['HX-Trigger'] = json.dumps({'refresh-conversations': {'phone': phone}})
                return response

            current_state = fetch_current_state()
            return render_template(
                'partials/conversation_edit_modal.html',
                phone=phone,
                current_state=current_state,
                states=allowed_states
            )
        finally:
            try:
                db.close()
            except Exception:
                pass

    @app.route('/conversations/<phone>/export')
    @login_required
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
