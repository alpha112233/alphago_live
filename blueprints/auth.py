import os
import re
import secrets

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    make_response,
    redirect,
    request,
    session,
    url_for,
)
from flask_wtf.csrf import generate_csrf

from database.auth_db import auth_cache, feed_token_cache, upsert_auth
from database.settings_db import get_smtp_settings, set_smtp_settings
from database.user_db import (  # Import the function
    User,
    authenticate_user,
    db_session,
    find_user_by_email,
    find_user_by_username,
)
from extensions import socketio
from limiter import limiter  # Import the limiter instance
from utils.email_debug import debug_smtp_connection
from utils.email_utils import send_password_reset_email, send_test_email
from utils.ip_helper import get_real_ip
from utils.logging import get_logger
from utils.session import check_session_validity

# Initialize logger
logger = get_logger(__name__)

# Access environment variables
LOGIN_RATE_LIMIT_MIN = os.getenv("LOGIN_RATE_LIMIT_MIN", "5 per minute")
LOGIN_RATE_LIMIT_HOUR = os.getenv("LOGIN_RATE_LIMIT_HOUR", "25 per hour")
RESET_RATE_LIMIT = os.getenv("RESET_RATE_LIMIT", "15 per hour")  # Password reset rate limit

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


@auth_bp.errorhandler(429)
def ratelimit_handler(e):
    return jsonify(status="error", message="Too many login attempts. Please wait a minute and try again."), 429


@auth_bp.route("/csrf-token", methods=["GET"])
def get_csrf_token():
    """Return a CSRF token for React SPA to use in form submissions."""
    token = generate_csrf()
    return jsonify({"csrf_token": token})


@auth_bp.route("/broker-config", methods=["GET"])
def get_broker_config():
    """Return broker configuration for React SPA.

    broker_name is always returned (needed to display the broker login button).
    broker_api_key and redirect_url are only returned when authenticated.
    """
    REDIRECT_URL = os.getenv("REDIRECT_URL")

    # Extract broker name from redirect URL
    match = re.search(r"/([^/]+)/callback$", REDIRECT_URL)
    broker_name = match.group(1) if match else None

    if not broker_name:
        return jsonify({"status": "error", "message": "Broker not configured"}), 500

    # Return full config only for authenticated users
    if "user" in session:
        BROKER_API_KEY = os.getenv("BROKER_API_KEY")
        return jsonify(
            {
                "status": "success",
                "broker_name": broker_name,
                "broker_api_key": BROKER_API_KEY,
                "redirect_url": REDIRECT_URL,
            }
        )

    # Unauthenticated: return broker name only so the login button is visible
    return jsonify(
        {
            "status": "success",
            "broker_name": broker_name,
            "broker_api_key": None,
            "redirect_url": REDIRECT_URL,
        }
    )


@auth_bp.route("/check-setup", methods=["GET"])
def check_setup_required():
    """Check if initial setup is required (no users exist)."""
    needs_setup = find_user_by_username() is None
    return jsonify({"status": "success", "needs_setup": needs_setup})


def _try_resume_broker_session(username):
    """
    Check if the user has an existing valid broker session in the DB.
    If so, validate it with a lightweight funds API call and resume
    the session without requiring broker OAuth re-authentication.

    Returns a JSON response if session was resumed, or None to proceed
    with normal broker OAuth flow.
    """
    from database.auth_db import Auth, decrypt_token, get_auth_token_dbquery

    try:
        auth_obj = get_auth_token_dbquery(username)
        if not auth_obj or auth_obj.is_revoked:
            return None

        # Decrypt the stored broker token
        auth_token = decrypt_token(auth_obj.auth)
        if not auth_token:
            return None

        broker = auth_obj.broker

        # alphago_live multi-broker fix: auth_db tracks "last broker authed"
        # (legacy single-broker), while broker_creds_db tracks "which broker
        # the customer wants to use right now". If the customer switched
        # their active broker (Edit → Make Active) but auth_db still has
        # the OLD broker's row, resuming that row would force the customer
        # back through the old broker's OAuth instead of the new one. Skip
        # the resume entirely when the two disagree — the broker-select
        # page will then route the customer to the right OAuth.
        try:
            from database.user_db import db_session, User
            from database.broker_creds_db import get_active_broker
            user = db_session.query(User).filter_by(username=username).first()
            if user is not None:
                active_in_creds = get_active_broker(user.id)
                if active_in_creds and active_in_creds != broker:
                    logger.info(
                        f"resume skipped: auth_db has stale '{broker}' session "
                        f"but broker_creds_db says active broker is "
                        f"'{active_in_creds}'. Customer will be routed to "
                        f"{active_in_creds}'s OAuth instead."
                    )
                    return None
        except Exception:
            # If the cross-check itself fails, don't block the legacy resume
            # path — keep the prior behaviour.
            logger.exception("broker_creds_db cross-check failed (continuing legacy resume)")
        feed_token = decrypt_token(auth_obj.feed_token) if auth_obj.feed_token else None
        user_id = auth_obj.user_id

        # Validate token with a lightweight broker API call (funds)
        import importlib
        try:
            broker_module = importlib.import_module(f"broker.{broker}.api.funds")
            funds_data = broker_module.get_margin_data(auth_token)
            # get_margin_data returns {} on failure (doesn't raise) — treat empty as invalid
            if not funds_data:
                logger.info(f"Broker token expired or invalid for {username} (empty funds response)")
                return None
        except Exception as e:
            logger.info(f"Broker token validation failed for {username}: {e}")
            return None

        # Token is valid — resume the session via handle_auth_success
        logger.info(f"Resuming existing broker session for {username} (broker: {broker})")

        from utils.auth_utils import handle_auth_success
        # Call handle_auth_success for its side effects (session setup, DB upsert,
        # master contract loading) but ignore its response format — the login
        # endpoint must always return JSON for the React frontend's fetch() call.
        try:
            handle_auth_success(
                auth_token=auth_token,
                user_session_key=username,
                broker=broker,
                feed_token=feed_token,
                user_id=user_id,
            )
        except Exception as e:
            logger.error(f"handle_auth_success failed during resume: {e}", exc_info=True)
            # Clear partial session state and fall through to OAuth
            session.pop("logged_in", None)
            session.pop("broker", None)
            session.pop("session_id", None)
            return None

        logger.info(f"Session resume complete for {username}, redirecting to dashboard")
        return jsonify({
            "status": "success",
            "message": "Broker session resumed",
            "redirect": "/dashboard",
            "broker": broker,
        }), 200

    except Exception as e:
        logger.error(f"Error trying to resume broker session: {e}", exc_info=True)
        return None


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit(LOGIN_RATE_LIMIT_MIN)
@limiter.limit(LOGIN_RATE_LIMIT_HOUR)
def login():
    # Handle POST requests first (for React SPA / AJAX login)
    if request.method == "POST":
        logger.info(f"[LOGIN] POST from IP={get_real_ip()}, UA={request.headers.get('User-Agent', '')[:80]}")
        logger.info(f"[LOGIN] Session state: user={session.get('user')}, logged_in={session.get('logged_in')}, broker={session.get('broker')}")

        # Check if setup is required
        if find_user_by_username() is None:
            logger.info("[LOGIN] No users exist, redirecting to setup")
            return jsonify(
                {
                    "status": "error",
                    "message": "Please complete initial setup first.",
                    "redirect": "/setup",
                }
            ), 400

        # Check if already logged in (check logged_in first — it means
        # broker auth is complete; "user" alone means only password was done)
        if session.get("logged_in"):
            logger.info(f"[LOGIN] Already fully logged in, redirecting to /dashboard")
            return jsonify(
                {"status": "success", "message": "Already logged in", "redirect": "/dashboard"}
            ), 200

        if "user" in session:
            logger.info(f"[LOGIN] User in session but not logged_in, redirecting to /broker")
            return jsonify(
                {"status": "success", "message": "Already logged in", "redirect": "/broker"}
            ), 200

        username = request.form["username"]
        password = request.form["password"]

        ip = get_real_ip()
        ua = request.headers.get("User-Agent", "")

        if authenticate_user(username, password):
            session["user"] = username  # Set the username in the session
            logger.info(f"[LOGIN] Password auth success for: {username}")
            try:
                from utils.audit import audit_log
                audit_log(actor="customer", action="session.login", resource=username,
                          src_ip=ip, status="ok", note=ua[:200])
            except Exception:
                pass

            # Try to resume existing broker session (skip OAuth if token still valid)
            resumed = _try_resume_broker_session(username)
            logger.info(f"[LOGIN] Resume result: {resumed is not None}, type={type(resumed).__name__ if resumed else 'None'}")
            if resumed:
                logger.info(f"[LOGIN] Returning resume response to frontend")
                from database.auth_db import log_login_attempt
                log_login_attempt(username, ip, ua, status="success",
                                  login_type="resume", broker=session.get("broker"))
                return resumed

            # No valid broker session — redirect to broker login
            logger.info(f"[LOGIN] No valid broker session, redirecting to /broker")
            from database.auth_db import log_login_attempt
            log_login_attempt(username, ip, ua, status="success", login_type="password")
            return jsonify({"status": "success"}), 200
        else:
            from database.auth_db import log_login_attempt
            log_login_attempt(username, get_real_ip(),
                              request.headers.get("User-Agent", ""),
                              status="failed", login_type="password",
                              failure_reason="invalid_credentials")
            return jsonify({"status": "error", "message": "Invalid credentials"}), 401

    # Handle GET requests - redirect to React frontend
    if find_user_by_username() is None:
        return redirect("/setup")

    if "user" in session:
        return redirect("/broker")

    if session.get("logged_in"):
        return redirect("/dashboard")

    return redirect("/login")


@auth_bp.route("/broker", methods=["GET", "POST"])
@limiter.limit(LOGIN_RATE_LIMIT_MIN)
@limiter.limit(LOGIN_RATE_LIMIT_HOUR)
def broker_login():
    if session.get("logged_in"):
        return redirect("/dashboard")
    if request.method == "GET":
        if "user" not in session:
            return redirect("/login")

        # Redirect to React broker selection page
        return redirect("/broker")


@auth_bp.route("/reset-password", methods=["GET", "POST"])
@limiter.limit(RESET_RATE_LIMIT)  # Password reset rate limit
def reset_password():
    # GET requests are handled by React frontend - redirect there
    if request.method == "GET":
        return redirect("/reset-password")

    # Handle JSON requests from React frontend
    if request.is_json:
        data = request.get_json()
        step = data.get("step")
        email = data.get("email")
    else:
        # Fall back to form data for compatibility
        step = request.form.get("step")
        email = request.form.get("email")

    # Debug logging for CSRF issues
    logger.debug(f"Password reset step: {step}, Session: {session.keys()}")

    if step == "email":
        user = find_user_by_email(email)

        # Always show the same response to prevent user enumeration
        if user:
            session["reset_email"] = email

        # Return success regardless of whether email exists (prevents enumeration)
        return jsonify({"status": "success", "message": "Email verified"})

    elif step == "select_totp":
        session["reset_method"] = "totp"
        return jsonify({"status": "success", "method": "totp"})

    elif step == "select_email":
        user = find_user_by_email(email)
        session["reset_method"] = "email"

        # Check if SMTP is configured
        smtp_settings = get_smtp_settings()
        if not smtp_settings or not smtp_settings.get("smtp_server"):
            return jsonify(
                {
                    "status": "error",
                    "message": "Email reset is not available. Please use TOTP authentication.",
                }
            ), 400

        if user:
            try:
                # Generate a secure token for the email reset
                token = secrets.token_urlsafe(32)
                session["reset_token"] = token
                session["reset_email"] = email

                # Create reset link
                reset_link = url_for("auth.reset_password_email", token=token, _external=True)
                send_password_reset_email(email, reset_link, user.username)
                logger.info(f"Password reset email sent to {email}")

            except Exception as e:
                logger.exception(f"Failed to send password reset email to {email}: {e}")
                return jsonify(
                    {
                        "status": "error",
                        "message": "Failed to send reset email. Please try TOTP authentication instead.",
                    }
                ), 500

        # Return success regardless of whether email exists (prevents enumeration)
        return jsonify({"status": "success", "message": "Reset email sent if account exists"})

    elif step == "totp":
        if request.is_json:
            totp_code = data.get("totp_code")
        else:
            totp_code = request.form.get("totp_code")

        user = find_user_by_email(email)

        if user and user.verify_totp(totp_code):
            # Generate a secure token for the password reset
            token = secrets.token_urlsafe(32)
            session["reset_token"] = token
            session["reset_email"] = email

            return jsonify({"status": "success", "message": "TOTP verified", "token": token})
        else:
            return jsonify(
                {"status": "error", "message": "Invalid TOTP code. Please try again."}
            ), 400

    elif step == "password":
        if request.is_json:
            token = data.get("token")
            password = data.get("password")
        else:
            token = request.form.get("token")
            password = request.form.get("password")

        # Verify token from session (handles both TOTP and email reset tokens)
        valid_token = token == session.get("reset_token") or token == session.get(
            "email_reset_token"
        )
        if not valid_token or email != session.get("reset_email"):
            return jsonify({"status": "error", "message": "Invalid or expired reset token."}), 400

        # Validate password strength
        from utils.auth_utils import validate_password_strength

        is_valid, error_message = validate_password_strength(password)
        if not is_valid:
            return jsonify({"status": "error", "message": error_message}), 400

        user = find_user_by_email(email)
        if user:
            user.set_password(password)
            db_session.commit()

            # Security: a password reset means we cannot trust any other
            # active session for this account. Kick every device — the
            # operator (or attacker) chose the reset path because they
            # could prove control of the email/TOTP, not because every
            # logged-in browser is theirs. Force re-login everywhere.
            from database.auth_db import clear_user_sessions
            clear_user_sessions(user.username)
            socketio.emit("force_logout", {
                "message": "Your password was reset. Please log in again with the new password.",
            })

            # Clear reset session data for security
            session.pop("reset_token", None)
            session.pop("reset_email", None)
            session.pop("reset_method", None)
            session.pop("email_reset_token", None)

            return jsonify(
                {"status": "success", "message": "Your password has been reset successfully."}
            )
        else:
            return jsonify({"status": "error", "message": "Error resetting password."}), 400

    return jsonify({"status": "error", "message": "Invalid step"}), 400


@auth_bp.route("/reset-password-email/<token>", methods=["GET"])
def reset_password_email(token):
    """Handle password reset via email link - validates token and redirects to React"""
    try:
        # Validate the token format
        if not token or len(token) != 43:  # URL-safe base64 tokens are 43 chars for 32 bytes
            flash("Invalid reset link.", "error")
            return redirect("/reset-password?error=invalid_link")

        # Check if this token was issued (stored in session during email send)
        if token != session.get("reset_token"):
            flash("Invalid or expired reset link.", "error")
            return redirect("/reset-password?error=expired_link")

        # Get the email associated with this reset token
        reset_email = session.get("reset_email")
        if not reset_email:
            flash("Reset session expired. Please start again.", "error")
            return redirect("/reset-password?error=session_expired")

        # Set up session for password reset (email verification counts as verified)
        session["email_reset_token"] = token

        # Redirect to React password reset page with token and email in URL
        # React will read these and show the password form
        return redirect(f"/reset-password?token={token}&email={reset_email}&verified=true")

    except Exception as e:
        logger.exception(f"Error processing email reset link: {e}")
        flash("Invalid or expired reset link.", "error")
        return redirect("/reset-password?error=processing_error")


@auth_bp.route("/change", methods=["GET", "POST"])
@check_session_validity
def change_password():
    if "user" not in session:
        # If the user is not logged in, redirect to login page
        if request.is_json:
            return jsonify({"status": "error", "message": "Not authenticated"}), 401
        return redirect("/login")

    # GET requests redirect to React profile page
    if request.method == "GET":
        return redirect("/profile")

    # Handle POST requests - change password
    # Support both JSON and form data
    if request.is_json:
        data = request.get_json()
        old_password = data.get("old_password") or data.get("current_password")
        new_password = data.get("new_password")
        confirm_password = data.get("confirm_password", new_password)
    else:
        old_password = request.form.get("old_password") or request.form.get("current_password")
        new_password = request.form.get("new_password")
        confirm_password = request.form.get("confirm_password", new_password)

    username = session["user"]
    user = User.query.filter_by(username=username).first()

    if user and user.check_password(old_password):
        if new_password == confirm_password:
            # Validate password strength
            from utils.auth_utils import validate_password_strength

            is_valid, error_message = validate_password_strength(new_password)
            if not is_valid:
                return jsonify({"status": "error", "message": error_message}), 400

            user.set_password(new_password)
            db_session.commit()

            # Security: a password change is a strong signal of suspected
            # compromise (or routine rotation). Either way, every active
            # session for this account should be re-authenticated. Kick all
            # devices — including the current one — and let the user log
            # in again with the new password. This prevents an attacker
            # who already has a valid cookie from continuing to hold it.
            from database.auth_db import clear_user_sessions
            clear_user_sessions(username)
            socketio.emit("force_logout", {
                "message": "Your password was changed. Please log in again with the new password.",
            })
            session.clear()

            return jsonify(
                {"status": "success", "message": "Your password has been changed successfully."}
            )
        else:
            return jsonify(
                {"status": "error", "message": "New password and confirm password do not match."}
            ), 400
    else:
        return jsonify({"status": "error", "message": "Current password is incorrect."}), 400


@auth_bp.route("/smtp-config", methods=["POST"])
@check_session_validity
def configure_smtp():
    if "user" not in session:
        # For AJAX requests, return JSON
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
            return jsonify({"status": "error", "message": "Not authenticated"}), 401
        flash("You must be logged in to configure SMTP settings.", "warning")
        return redirect(url_for("auth.login"))

    try:
        smtp_server = request.form.get("smtp_server")
        smtp_port = int(request.form.get("smtp_port", 587))
        smtp_username = request.form.get("smtp_username")
        smtp_password = request.form.get("smtp_password")
        smtp_use_tls = request.form.get("smtp_use_tls") == "on"
        smtp_from_email = request.form.get("smtp_from_email")
        smtp_helo_hostname = request.form.get("smtp_helo_hostname")

        # Only update password if provided
        if smtp_password and smtp_password.strip():
            set_smtp_settings(
                smtp_server=smtp_server,
                smtp_port=smtp_port,
                smtp_username=smtp_username,
                smtp_password=smtp_password,
                smtp_use_tls=smtp_use_tls,
                smtp_from_email=smtp_from_email,
                smtp_helo_hostname=smtp_helo_hostname,
            )
        else:
            # Update without password change
            set_smtp_settings(
                smtp_server=smtp_server,
                smtp_port=smtp_port,
                smtp_username=smtp_username,
                smtp_use_tls=smtp_use_tls,
                smtp_from_email=smtp_from_email,
                smtp_helo_hostname=smtp_helo_hostname,
            )

        logger.info(f"SMTP settings updated by user: {session['user']}")

        # For AJAX requests, return JSON
        if (
            request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or request.is_json
            or "multipart/form-data" in request.content_type
        ):
            return jsonify({"status": "success", "message": "SMTP settings updated successfully"})

        flash("SMTP settings updated successfully.", "success")

    except Exception as e:
        logger.exception(f"Error updating SMTP settings: {str(e)}")
        # For AJAX requests, return JSON
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
            return jsonify(
                {"status": "error", "message": f"Error updating SMTP settings: {str(e)}"}
            ), 500
        flash(f"Error updating SMTP settings: {str(e)}", "error")

    return redirect(url_for("auth.change_password") + "?tab=smtp")


@auth_bp.route("/test-smtp", methods=["POST"])
@check_session_validity
def test_smtp():
    if "user" not in session:
        return jsonify(
            {"success": False, "message": "You must be logged in to test SMTP settings."}
        ), 401

    try:
        test_email = request.form.get("test_email")
        if not test_email:
            return jsonify(
                {"success": False, "message": "Please provide a test email address."}
            ), 400

        # Validate email format
        email_pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        if not re.match(email_pattern, test_email):
            return jsonify(
                {"success": False, "message": "Please provide a valid email address."}
            ), 400

        # Send test email
        result = send_test_email(test_email, sender_name=session["user"])

        if result["success"]:
            logger.info(f"Test email sent successfully by user: {session['user']} to {test_email}")
            return jsonify({"success": True, "message": result["message"]}), 200
        else:
            logger.warning(f"Test email failed for user: {session['user']} - {result['message']}")
            return jsonify({"success": False, "message": result["message"]}), 400

    except Exception as e:
        error_msg = f"Error sending test email: {str(e)}"
        logger.exception(f"Test email error for user {session['user']}: {e}")
        return jsonify({"success": False, "message": error_msg}), 500


@auth_bp.route("/debug-smtp", methods=["POST"])
@check_session_validity
def debug_smtp():
    if "user" not in session:
        return jsonify(
            {"success": False, "message": "You must be logged in to debug SMTP settings."}
        ), 401

    try:
        logger.info(f"SMTP debug requested by user: {session['user']}")
        result = debug_smtp_connection()

        return jsonify(
            {
                "success": result["success"],
                "message": result["message"],
                "details": result["details"],
            }
        ), 200

    except Exception as e:
        error_msg = f"Error debugging SMTP: {str(e)}"
        logger.exception(f"SMTP debug error for user {session['user']}: {e}")
        return jsonify(
            {"success": False, "message": error_msg, "details": [f"Unexpected error: {e}"]}
        ), 500


@auth_bp.route("/session-status", methods=["GET"])
def get_session_status():
    """Return current session status for React SPA."""
    if "user" not in session:
        # Return 200 with authenticated: false instead of 401
        # This prevents unnecessary console errors in the browser
        return jsonify(
            {"status": "success", "message": "Not authenticated", "authenticated": False, "logged_in": False}
        ), 200

    # If session claims to be logged in with broker, validate the auth token exists
    if session.get("logged_in") and session.get("broker"):
        from database.auth_db import get_api_key_for_tradingview, get_auth_token

        auth_token = get_auth_token(session.get("user"))
        if auth_token is None:
            logger.warning(
                f"Session status: stale session detected for user {session.get('user')} - no auth token"
            )
            # Clear the stale session
            session.clear()
            return jsonify(
                {"status": "success", "message": "Session expired", "authenticated": False, "logged_in": False}
            ), 200

        # Get API key for the user
        api_key = get_api_key_for_tradingview(session.get("user"))

        # Include active session count
        from database.auth_db import get_active_sessions
        active_count = len(get_active_sessions(session.get("user")))

        return jsonify(
            {
                "status": "success",
                "authenticated": True,
                "logged_in": session.get("logged_in", False),
                "user": session.get("user"),
                "broker": session.get("broker"),
                "api_key": api_key,
                "active_sessions": active_count,
            }
        )

    # Include active session count
    from database.auth_db import get_active_sessions, get_api_key_for_tradingview
    active_count = len(get_active_sessions(session.get("user")))

    return jsonify(
        {
            "status": "success",
            "authenticated": True,
            "logged_in": session.get("logged_in", False),
            "user": session.get("user"),
            "broker": session.get("broker"),
            # Needed by the SPA even without a broker session: in analyze
            # (sandbox) mode the orderbook/positions/tradebook pages call
            # /api/v1/* with this key. Omitting it left the frontend store
            # empty → those pages silently rendered "No orders today"
            # (found live 2026-06-11 on test.hostingsol).
            "api_key": get_api_key_for_tradingview(session.get("user")),
            "active_sessions": active_count,
        }
    )


@auth_bp.route("/active-sessions", methods=["GET"])
@check_session_validity
def active_sessions():
    """Return the list of active sessions for the current user."""
    if "user" not in session:
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    from database.auth_db import get_active_sessions
    sessions = get_active_sessions(session["user"])
    current_session_id = session.get("session_id")

    return jsonify({
        "status": "success",
        "count": len(sessions),
        "current_session_id": current_session_id,
        "sessions": sessions,
    })


@auth_bp.route("/app-info", methods=["GET"])
def get_app_info():
    """Return app information including version for React SPA."""
    from utils.version import get_version

    return jsonify({"status": "success", "version": get_version(), "name": "OpenAlgo"})


@auth_bp.route("/analyzer-mode", methods=["GET"])
@check_session_validity
def get_analyzer_mode_status():
    """Return current analyzer mode status for React SPA."""
    if "user" not in session:
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    try:
        from database.settings_db import get_analyze_mode

        current_mode = get_analyze_mode()

        return jsonify(
            {
                "status": "success",
                "data": {
                    "mode": "analyze" if current_mode else "live",
                    "analyze_mode": current_mode,
                },
            }
        )
    except Exception as e:
        logger.exception(f"Error getting analyzer mode: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@auth_bp.route("/analyzer-toggle", methods=["POST"])
@check_session_validity
def toggle_analyzer_mode_session():
    """Toggle analyzer mode for React SPA using session authentication."""
    if "user" not in session:
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    if not session.get("logged_in"):
        return jsonify({"status": "error", "message": "Broker not connected"}), 401

    try:
        from database.settings_db import get_analyze_mode, set_analyze_mode

        # Get current mode and toggle it
        current_mode = get_analyze_mode()
        new_mode = not current_mode

        # Set the new mode
        set_analyze_mode(new_mode)

        # Start/stop execution engine and squareoff scheduler based on mode
        from sandbox.execution_thread import start_execution_engine, stop_execution_engine
        from sandbox.squareoff_thread import start_squareoff_scheduler, stop_squareoff_scheduler

        if new_mode:
            # Analyzer mode ON - start both threads
            start_execution_engine()
            start_squareoff_scheduler()

            # Run catch-up settlement for any missed settlements while app was stopped
            from sandbox.position_manager import catchup_missed_settlements

            try:
                catchup_missed_settlements()
                logger.info("Catch-up settlement check completed")
            except Exception as e:
                logger.exception(f"Error in catch-up settlement: {e}")

            logger.info("Analyzer mode enabled - Execution engine and square-off scheduler started")
        else:
            # Analyzer mode OFF - stop both threads
            stop_execution_engine()
            stop_squareoff_scheduler()
            logger.info(
                "Analyzer mode disabled - Execution engine and square-off scheduler stopped"
            )

        return jsonify(
            {
                "status": "success",
                "data": {
                    "mode": "analyze" if new_mode else "live",
                    "analyze_mode": new_mode,
                    "message": f"Switched to {'Analyze' if new_mode else 'Live'} mode",
                },
            }
        )

    except Exception as e:
        logger.exception(f"Error toggling analyzer mode: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@auth_bp.route("/dashboard-data", methods=["GET"])
@check_session_validity
def get_dashboard_data():
    """Return dashboard funds data using session authentication for React SPA."""
    if "user" not in session:
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    if not session.get("logged_in"):
        return jsonify({"status": "error", "message": "Broker not connected"}), 401

    login_username = session["user"]
    broker = session.get("broker")

    if not broker:
        return jsonify({"status": "error", "message": "Broker not set in session"}), 400

    try:
        from database.auth_db import get_api_key_for_tradingview, get_auth_token
        from database.settings_db import get_analyze_mode
        from services.funds_service import get_funds

        AUTH_TOKEN = get_auth_token(login_username)

        if AUTH_TOKEN is None:
            logger.warning(f"No auth token found for user {login_username}")
            return jsonify({"status": "error", "message": "Session expired"}), 401

        # Check if in analyze mode
        if get_analyze_mode():
            api_key = get_api_key_for_tradingview(login_username)
            if api_key:
                success, response, status_code = get_funds(api_key=api_key)
            else:
                return jsonify(
                    {"status": "error", "message": "API key required for analyze mode"}
                ), 400
        else:
            success, response, status_code = get_funds(auth_token=AUTH_TOKEN, broker=broker)

        if not success:
            logger.error(f"Failed to get funds data: {response.get('message', 'Unknown error')}")
            return jsonify(
                {"status": "error", "message": response.get("message", "Failed to get funds")}
            ), status_code

        margin_data = response.get("data", {})

        if not margin_data:
            # GRACEFUL DEGRADE: a transient margin/funds failure (e.g. a
            # momentarily degraded broker session) must NOT 500 the whole
            # dashboard — that blanks the React SPA, so the orderbook /
            # tradebook / positions panels (which come from the INTERACTIVE
            # token and are fine) also render empty. Return 200 with an empty
            # margin payload + a flag so the funds panel shows "unavailable"
            # while the rest of the dashboard still loads. (2026-07-10.)
            logger.warning(
                f"margin data empty for user {login_username} — returning "
                f"degraded dashboard (funds unavailable, other panels unaffected)"
            )
            return jsonify({
                "status": "success",
                "data": {},
                "margin_available": False,
                "message": "Funds/margin temporarily unavailable — reconnect the "
                           "broker if this persists.",
            })

        return jsonify({"status": "success", "data": margin_data, "margin_available": True})

    except Exception as e:
        logger.exception(f"Error fetching dashboard data: {e}")
        return jsonify({"status": "error", "message": "Internal server error"}), 500


@auth_bp.route("/logout", methods=["GET", "POST"])
def logout():
    if session.get("logged_in"):
        username = session["user"]

        # Clear cache entries before database update to prevent stale data access
        cache_key_auth = f"auth-{username}"
        cache_key_feed = f"feed-{username}"
        if cache_key_auth in auth_cache:
            del auth_cache[cache_key_auth]
            logger.info(f"Cleared auth cache for user: {username}")
        if cache_key_feed in feed_token_cache:
            del feed_token_cache[cache_key_feed]
            logger.info(f"Cleared feed token cache for user: {username}")

        # Clear symbol cache on logout
        try:
            from database.master_contract_cache_hook import clear_cache_on_logout

            clear_cache_on_logout()
            logger.info("Cleared symbol cache on logout")
        except Exception as cache_error:
            logger.exception(f"Error clearing symbol cache on logout: {cache_error}")

        # writing to database
        inserted_id = upsert_auth(username, "", "", revoke=True)
        if inserted_id is not None:
            logger.info(f"Database Upserted record with ID: {inserted_id}")
            logger.info(f"Auth Revoked in the Database for user: {username}")
        else:
            logger.error(f"Failed to upsert auth token for user: {username}")

        # Clear ALL sessions for this user (logout means all devices)
        from database.auth_db import clear_user_sessions
        clear_user_sessions(username)

        # Notify all connected devices to logout immediately
        socketio.emit("force_logout", {
            "message": "You have been logged out from another device.",
        })

        # Update session count to 0
        socketio.emit("active_sessions_update", {
            "count": 0,
            "sessions": [],
        })

        # Clear entire session to ensure complete logout
        session.clear()
        logger.info(f"Session cleared for user: {username}")

    # For POST requests (AJAX from React), return JSON
    if request.method == "POST":
        return jsonify({"status": "success", "message": "Logged out successfully"})

    # For GET requests (traditional), redirect to login page
    return redirect(url_for("auth.login"))


@auth_bp.route("/profile-data", methods=["GET"])
@check_session_validity
def get_profile_data():
    """Return profile data for React SPA."""
    if "user" not in session:
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    username = session["user"]

    try:
        # Get SMTP settings
        smtp_settings = get_smtp_settings()

        # Mask SMTP password - just indicate if it's set
        if smtp_settings and smtp_settings.get("smtp_password"):
            smtp_settings = dict(smtp_settings)
            smtp_settings["smtp_password"] = True
        elif smtp_settings:
            smtp_settings = dict(smtp_settings)
            smtp_settings["smtp_password"] = False

        # Generate TOTP QR code
        user = User.query.filter_by(username=username).first()
        qr_code = None
        totp_secret = None

        if user:
            try:
                import base64
                import io

                import qrcode

                qr = qrcode.QRCode(version=1, box_size=10, border=5)
                qr.add_data(user.get_totp_uri())
                qr.make(fit=True)

                img_buffer = io.BytesIO()
                qr.make_image(fill_color="black", back_color="white").save(img_buffer, format="PNG")
                qr_code = base64.b64encode(img_buffer.getvalue()).decode()
                # Use the public getter that decrypts the at-rest ciphertext.
                # `user.totp_secret` is the raw column value (ciphertext);
                # `get_totp_secret()` returns the plaintext with a fallback
                # for pre-migration rows.
                totp_secret = user.get_totp_secret()
            except Exception as e:
                logger.exception(f"Error generating TOTP QR code: {e}")

        return jsonify(
            {
                "status": "success",
                "data": {
                    "username": username,
                    "smtp_settings": smtp_settings,
                    "qr_code": qr_code,
                    "totp_secret": totp_secret,
                },
            }
        )

    except Exception as e:
        logger.exception(f"Error getting profile data: {e}")
        return jsonify({"status": "error", "message": "Failed to get profile data"}), 500


@auth_bp.route("/change-password", methods=["POST"])
@check_session_validity
def change_password_api():
    """Change password API endpoint for React SPA."""
    if "user" not in session:
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    username = session["user"]
    old_password = request.form.get("old_password")
    new_password = request.form.get("new_password")
    confirm_password = request.form.get("confirm_password")

    if not all([old_password, new_password, confirm_password]):
        return jsonify({"status": "error", "message": "All fields are required"}), 400

    user = User.query.filter_by(username=username).first()

    if not user or not user.check_password(old_password):
        return jsonify({"status": "error", "message": "Current password is incorrect"}), 400

    if new_password != confirm_password:
        return jsonify({"status": "error", "message": "New passwords do not match"}), 400

    # Validate password strength
    from utils.auth_utils import validate_password_strength

    is_valid, error_message = validate_password_strength(new_password)
    if not is_valid:
        return jsonify({"status": "error", "message": error_message}), 400

    try:
        user.set_password(new_password)
        db_session.commit()
        logger.info(f"Password changed successfully for user: {username}")

        # Security: a password change should invalidate every active session
        # for this account, including the current browser. The user logs in
        # again with the new password — typical 5-second flow — and any
        # attacker holding a stolen cookie is kicked out at the same moment.
        from database.auth_db import clear_user_sessions
        clear_user_sessions(username)
        socketio.emit("force_logout", {
            "message": "Your password was changed. Please log in again with the new password.",
        })
        session.clear()

        return jsonify({"status": "success", "message": "Password changed successfully"})
    except Exception as e:
        logger.exception(f"Error changing password: {e}")
        return jsonify({"status": "error", "message": "Failed to change password"}), 500


# ---------------------------------------------------------------------------
# Account activation (one-time set-password link)
#
# Provisioning no longer emails a temp password. The provisioner creates the
# admin user with a random throwaway password, mints a single-use token
# (database.user_db.create_password_activation) and the welcome email links
# to /activate?token=... (React page) which drives this endpoint.
# ---------------------------------------------------------------------------


@auth_bp.route("/activate", methods=["POST"])
@limiter.limit(RESET_RATE_LIMIT)
def activate_account():
    """Two-step JSON API for the /activate page.

    step=validate {token}              → {"status","username"} (live token?)
    step=set      {token,new_password} → sets the password, burns the token
    """
    from database.user_db import (
        consume_password_activation,
        validate_password_activation,
    )

    data = request.get_json(silent=True) or request.form
    step = (data.get("step") or "").strip()
    token = (data.get("token") or "").strip()

    if step == "validate":
        username = validate_password_activation(token)
        if username is None:
            return jsonify(
                {
                    "status": "error",
                    "message": "This activation link is invalid, expired, or already used.",
                }
            ), 400
        return jsonify({"status": "success", "username": username})

    if step == "set":
        new_password = data.get("new_password") or ""
        from utils.auth_utils import validate_password_strength

        is_valid, error_message = validate_password_strength(new_password)
        if not is_valid:
            return jsonify({"status": "error", "message": error_message}), 400

        ok, result = consume_password_activation(token, new_password)
        if not ok:
            return jsonify({"status": "error", "message": result}), 400

        logger.info(f"[ACTIVATE] Password set via activation link for: {result}")
        try:
            from utils.audit import audit_log

            audit_log(
                actor="customer",
                action="auth.activate",
                resource=result,
                src_ip=get_real_ip(),
                status="ok",
            )
        except Exception:
            pass
        return jsonify({"status": "success", "username": result})

    return jsonify({"status": "error", "message": "Unknown step"}), 400


# ---------------------------------------------------------------------------
# "Sign in with Google" via the hostingsol central SSO broker.
#
# Google OAuth doesn't allow wildcard redirect URIs, so per-instance Google
# clients are impossible. Instead a CENTRAL broker (hostingsol landing, one
# Google client) runs the OAuth dance and redirects back here with a
# short-lived RS256 assertion we verify offline:
#
#   /auth/google/start ──▶ {SSO_BROKER_URL}/sso/google/start?instance=..&state=..
#       ──▶ Google ──▶ broker callback ──▶ /auth/google/callback?assertion=..
#
# Required env (stamped by the provisioner; all three or the feature is off):
#   SSO_BROKER_URL          e.g. https://hostingsol.alphaquark.in
#   SSO_INSTANCE_ID         this instance's subdomain (JWT audience)
#   SSO_JWT_PUBLIC_KEY_B64  base64(PEM) of the broker's RS256 public key
#
# The Google account's email must equal an existing local user's email —
# SSO is an alternative front door, never an account-creation path. Local
# password + TOTP keep working regardless (Google outage ≠ lockout).
# ---------------------------------------------------------------------------


def _sso_config():
    broker_url = (os.getenv("SSO_BROKER_URL") or "").strip().rstrip("/")
    instance_id = (os.getenv("SSO_INSTANCE_ID") or "").strip()
    pub_b64 = (os.getenv("SSO_JWT_PUBLIC_KEY_B64") or "").strip()
    if not (broker_url and instance_id and pub_b64):
        return None
    import base64

    try:
        public_key_pem = base64.b64decode(pub_b64).decode()
    except Exception:
        logger.error("SSO_JWT_PUBLIC_KEY_B64 is not valid base64 — SSO disabled")
        return None
    return {
        "broker_url": broker_url,
        "instance_id": instance_id,
        "public_key_pem": public_key_pem,
    }


@auth_bp.route("/sso-config", methods=["GET"])
def sso_config():
    """Tells the login page whether to render the Google button."""
    return jsonify({"google_enabled": _sso_config() is not None})


@auth_bp.route("/google/start", methods=["GET"])
@limiter.limit(LOGIN_RATE_LIMIT_HOUR)
def google_sso_start():
    cfg = _sso_config()
    if cfg is None:
        return redirect("/login?sso_error=Google+sign-in+is+not+configured")
    state = secrets.token_urlsafe(24)
    session["sso_state"] = state
    from urllib.parse import urlencode

    q = urlencode({"instance": cfg["instance_id"], "state": state})
    return redirect(f"{cfg['broker_url']}/sso/google/start?{q}")


@auth_bp.route("/google/callback", methods=["GET"])
@limiter.limit(LOGIN_RATE_LIMIT_HOUR)
def google_sso_callback():
    from urllib.parse import quote

    def _fail(msg):
        logger.warning(f"[SSO] Google sign-in rejected: {msg}")
        return redirect(f"/login?sso_error={quote(msg)}")

    cfg = _sso_config()
    if cfg is None:
        return _fail("Google sign-in is not configured")

    # Upstream (broker) error, e.g. user cancelled at Google.
    if request.args.get("error"):
        return _fail(request.args.get("error_description") or request.args["error"])

    assertion = request.args.get("assertion") or ""
    state = request.args.get("state") or ""
    expected_state = session.pop("sso_state", None)
    if not expected_state or state != expected_state:
        return _fail("Sign-in session expired — please try again")

    import jwt as pyjwt

    try:
        claims = pyjwt.decode(
            assertion,
            cfg["public_key_pem"],
            algorithms=["RS256"],
            audience=cfg["instance_id"],
            issuer="hostingsol-sso",
            leeway=10,
            options={"require": ["exp", "aud", "iss"]},
        )
    except Exception as e:
        return _fail(f"Could not verify sign-in assertion ({type(e).__name__})")

    # Bind the assertion to the browser session that started the flow.
    if claims.get("nonce") != expected_state:
        return _fail("Sign-in state mismatch — please try again")
    if not claims.get("email_verified"):
        return _fail("Google account email is not verified")

    email = (claims.get("email") or "").strip().lower()
    user = None
    for candidate in User.query.all():
        if (candidate.email or "").strip().lower() == email:
            user = candidate
            break
    if user is None:
        return _fail(
            "This Google account doesn't match this server's admin email. "
            "Use your username and password instead."
        )

    session["user"] = user.username
    logger.info(f"[SSO] Google sign-in success for: {user.username}")
    try:
        from database.auth_db import log_login_attempt

        log_login_attempt(
            user.username,
            get_real_ip(),
            request.headers.get("User-Agent", ""),
            status="success",
            login_type="sso_google",
        )
    except Exception:
        pass
    try:
        from utils.audit import audit_log

        audit_log(
            actor="customer",
            action="session.login",
            resource=user.username,
            src_ip=get_real_ip(),
            status="ok",
            note="google-sso",
        )
    except Exception:
        pass

    return redirect("/broker")
