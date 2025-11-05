import json
import logging
# app.py
from flask import Flask, request, render_template, redirect, url_for, current_app, make_response, jsonify
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from flask_wtf import FlaskForm
from flask_wtf.csrf import CSRFProtect
from wtforms import PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired
import os
import psutil
from dotenv import load_dotenv

from admin import Admin
from db import DB
from models import FSMState
from gpt import GPTClient, GPTServiceError, log_message_to_db
from logging_config import configure_logging
from reach_out import ReachOut
from twilio_test import TwilioSMSClient
from user_context import UserContext

csrf = CSRFProtect()


class LoginForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired()])
    password = PasswordField('Password', validators=[DataRequired()])
    submit = SubmitField('Log In')


# ----------------------------
# App factory & wiring
# ----------------------------

def create_app() -> Flask:
    load_dotenv()
    configure_logging()
    root_logger = logging.getLogger()
    app = Flask(__name__)
    app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret")
    csrf.init_app(app)

    app.logger.handlers = []
    for handler in root_logger.handlers:
        app.logger.addHandler(handler)
    app.logger.setLevel(root_logger.level)
    app.logger.propagate = False

    login_manager = LoginManager()
    login_manager.login_view = "login"
    login_manager.init_app(app)

    # Build dependencies once per process; store on app config
    twilio_sid = os.getenv("TWILIO_SID")
    twilio_token = os.getenv("TWILIO_TOKEN")
    twilio_messaging_sid = os.getenv("TWILIO_MESSAGINGID")

    twilio_client = TwilioSMSClient(
        twilio_sid,
        twilio_token,
        twilio_messaging_sid,
    )
    try:
        twilio_client.verify_credentials()
    except RuntimeError as exc:
        app.logger.warning("Twilio credentials not fully configured: %s", exc)
    except Exception as exc:
        app.logger.error("Twilio credential verification failed", exc_info=exc)
    else:
        app.logger.info("Twilio credentials verified successfully.")

    services = {
        "gpt": GPTClient(
            max_tokens=int(os.getenv("max_tokens", 200))
        ),
        "twilio": twilio_client,
        "admin_user": Admin(
            username=os.getenv("ADMIN_USERNAME", "admin"),
            password=os.getenv("ADMIN_PASSWORD"),
        ),
    }

    reach_out_limit_raw = os.getenv("REACH_OUT_MAX_ACTIVE")
    try:
        reach_out_limit = int(reach_out_limit_raw) if reach_out_limit_raw else None
    except ValueError:
        reach_out_limit = None

    services["reach_out"] = ReachOut(
        gpt_client=services["gpt"],
        twilio_client=services["twilio"],
        max_active_conversations=reach_out_limit,
    )
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
        current_app.logger.debug("Memory usage: %.2f MB", mem)

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

    @csrf.exempt
    @app.route("/sms", methods=["POST"])
    def sms_reply():
        # Inbound webhook from Twilio
        form_params = request.form.to_dict(flat=True)
        signature = request.headers.get("X-Twilio-Signature")
        services = get_services()
        twilio_client = services["twilio"]

        try:
            is_valid = twilio_client.validate_webhook(signature, request.url, form_params)
        except RuntimeError as exc:
            current_app.logger.error("Unable to validate Twilio webhook: %s", exc)
            return "Twilio signature validation misconfigured", 500

        if not is_valid:
            current_app.logger.warning(
                "Rejected Twilio webhook with invalid signature: from=%s sid=%s",
                form_params.get("From"),
                form_params.get("MessageSid"),
            )
            return "Invalid signature", 403

        incoming_msg = (form_params.get("Body") or "").strip()
        from_number = form_params.get("From")
        twilio_sid = form_params.get("MessageSid")

        current_app.logger.info("Incoming from %s: %s", from_number, incoming_msg)

        # Build request-scoped objects
        db = DB()
        try:
            # Record inbound
            db.insert_message(from_number, incoming_msg, twilio_sid)

            stop_keywords = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}
            normalized_msg = incoming_msg.lower()

            if normalized_msg in stop_keywords:
                try:
                    state = db.session.get(FSMState, from_number)
                    if state:
                        state.statename = "stop"
                    else:
                        db.session.add(
                            FSMState(
                                phone_number=from_number,
                                statename="stop",
                                was_interested=False,
                            )
                        )
                    db.session.commit()
                except Exception as exc:
                    current_app.logger.exception(
                        "Failed to set stop state for %s", from_number, exc_info=exc
                    )

                reply = "Messages Stopped"
                try:
                    log_message_to_db(db.session, from_number, reply)
                except Exception as record_exc:
                    current_app.logger.exception(
                        "Failed to record stop acknowledgement for %s",
                        from_number,
                        exc_info=record_exc,
                    )
            else:
                # Build a proper UserContext (fixes the previous string misuse)
                user_ctx = UserContext(str(from_number))

                # Generate reply via GPT
                gpt = services["gpt"]
                try:
                    reply = gpt.generate_response(incoming_msg, user_ctx, db)
                except GPTServiceError as exc:
                    fallback_reply = current_app.config.get(
                        "GPT_FALLBACK_MESSAGE",
                        "Sorry, we're having trouble replying automatically.",
                    )
                    current_app.logger.warning(
                        "GPT service unavailable for %s, using fallback message.",
                        from_number,
                        exc_info=exc,
                    )
                    reply = fallback_reply
                    try:
                        gpt.insert_with_db_instance(db, reply, user_ctx)
                    except Exception as record_exc:
                        current_app.logger.exception(
                            "Failed to record fallback reply for %s",
                            from_number,
                            exc_info=record_exc,
                        )
                except Exception as exc:
                    current_app.logger.exception(
                        "Unexpected failure generating response for %s",
                        from_number,
                    )
                    return ("Internal server error.", 500)

            # Send reply out-of-band via Twilio REST
            if reply and int(os.getenv("OUTBOUND_LIVE_TOGGLE", 0)) == 1:
                twilio_client.send_sms(to_phone=from_number, message=reply)

        finally:
            try:
                db.close()
            except Exception:
                pass

        # Return a simple 200 OK to Twilio (we already replied via REST)
        return "OK", 200

    @app.route('/reach-out/run', methods=['POST'])
    @login_required
    def reach_out_run():
        reach_out_service = get_services().get('reach_out')
        if reach_out_service is None:
            return jsonify({'error': 'reach_out service unavailable'}), 500

        def _parse_int(value, default=None):
            if value is None:
                return default
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        fetch_default = _parse_int(os.getenv('REACH_OUT_FETCH_LIMIT'), 20)
        fetch_limit = _parse_int(request.values.get('limit'), fetch_default)
        if fetch_limit is None or fetch_limit <= 0:
            return jsonify({'error': 'invalid limit'}), 400

        max_active_override = _parse_int(request.values.get('max_active'))

        db = DB()
        try:
            candidates = db.fetch_reach_out_candidates(limit=fetch_limit, exclude_states=('done',))
        finally:
            db.close()

        if not candidates:
            return jsonify({
                'status': 'idle',
                'reason': 'no candidates',
                'requested': fetch_limit,
            })

        outcome = reach_out_service.send_bulk(candidates, max_active=max_active_override)
        summary = outcome.get('summary', {}) if isinstance(outcome, dict) else {}
        results = outcome.get('results', []) if isinstance(outcome, dict) else outcome

        return jsonify({
            'status': 'ok',
            'run_id': outcome.get('run_id') if isinstance(outcome, dict) else None,
            'requested': summary.get('requested', fetch_limit),
            'processed': summary.get('processed', len(candidates)),
            'summary': summary,
            'results': results,
        })

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
            done_count = sum(1 for c in conversations if (c.get('status') or '').upper() == 'DONE')
            selected_phone = conversations[0]['phone_number'] if conversations else None
            messages = db.SQL_full_conversation_per_phone(selected_phone) if selected_phone else []
            return render_template(
                'index.html',
                conversations=conversations,
                selected_phone=selected_phone,
                conversations_count=len(conversations),
                done_count=done_count,
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

            current_app.logger.debug("Selected conversation: %s", phone)

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
        interested_states = {'interested', 'action_sqft'}
        db = DB()

        def fetch_current_state():
            state = db.session.get(FSMState, phone)
            return state.statename if state and state.statename else 'start'

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

                mark_interested = new_state in interested_states
                state = db.session.get(FSMState, phone)

                if state:
                    state.statename = new_state
                    state.was_interested = bool(state.was_interested) or mark_interested
                else:
                    state = FSMState(
                        phone_number=phone,
                        statename=new_state,
                        was_interested=mark_interested,
                    )
                    db.session.add(state)

                db.session.commit()

                if new_state == "stop":
                    stop_reply = "Messages Stopped"
                    try:
                        log_message_to_db(db.session, phone, stop_reply)
                    except Exception as record_exc:
                        current_app.logger.exception(
                            "Failed to record stop acknowledgement for %s via edit",
                            phone,
                            exc_info=record_exc,
                        )

                    if int(os.getenv("OUTBOUND_LIVE_TOGGLE", 0)) == 1:
                        try:
                            services = get_services()
                            twilio_client = services.get("twilio")
                            if twilio_client:
                                twilio_client.send_sms(to_phone=phone, message=stop_reply)
                        except Exception as send_exc:
                            current_app.logger.exception(
                                "Failed to send stop acknowledgement for %s via edit",
                                phone,
                                exc_info=send_exc,
                            )

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

